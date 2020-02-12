import kubernetes
from typing import Dict, List
from queue import SimpleQueue
from .event_handler import EventHandler
from .threaded_kubernetes_watch import (
    ThreadedKubernetesWatchNamspeace,
    ThreadedKubernetesWatchPodLog,
    ThreadedKubernetesWatch,
)


class ThreadedKubernetesObjectsWatcher(EventHandler):
    _object_yaml: str = None
    auto_watch_pod_logs: bool = True
    _log_reader: ThreadedKubernetesWatchPodLog = None
    _id: str = None
    _has_read_logs = False
    client: kubernetes.client.CoreV1Api = None
    _was_deleted: bool = False

    def __init__(self, client, object_yaml, auto_watch_pod_logs: bool = True):
        super().__init__()
        self.client = client
        self._id = ThreadedKubernetesObjectsWatcher.compose_object_id_from_yaml(object_yaml)
        self.auto_watch_pod_logs = auto_watch_pod_logs
        self._was_deleted = False

    def emit(self, name, *args, **kwargs):
        if len(args) < 2:
            args = list(args) + [self]
        super().emit(name, *args, **kwargs)

    def stop(self):
        if self._log_reader is not None and self._log_reader.is_streaming:
            self._log_reader.stop()

    @property
    def id(self):
        return self._id

    @property
    def kind(self):
        return self.yaml["kind"]

    @property
    def yaml(self) -> dict:
        return self._object_yaml

    @property
    def name(self) -> str:
        return self.yaml["metadata"]["name"]

    @property
    def namespace(self) -> str:
        return self.yaml["metadata"]["namespace"]

    @staticmethod
    def compose_object_id_from_yaml(object_yaml):
        return ThreadedKubernetesObjectsWatcher.compose_object_id_from_values(
            object_yaml["kind"], object_yaml["metadata"]["namespace"], object_yaml["metadata"]["name"],
        )

    @staticmethod
    def compose_object_id_from_values(kind, namespace, name):
        return "/".join([kind, namespace, name])

    @staticmethod
    def read_job_status_from_yaml(status_yaml, backoffLimit: int = 0):
        job_status = "Pending"
        if "startTime" in status_yaml:
            if "completionTime" in status_yaml:
                job_status = "Succeeded"
            elif "failed" in status_yaml and int(status_yaml["failed"]) > backoffLimit:
                job_status = "Failed"
            else:
                job_status = "Running"
        return job_status

    @property
    def status(self):
        if self.kind == "Service":
            return "Deleted" if self._was_deleted else "Active"
        elif self.kind == "Job":
            job_status = None
            if self._was_deleted:
                job_status = "Deleted"
            else:
                job_status = ThreadedKubernetesObjectsWatcher.read_job_status_from_yaml(
                    self.yaml["status"], int(self.yaml["spec"]["backoffLimit"])
                )

            return job_status
        elif self.kind == "Pod":
            pod_status = None
            if self._was_deleted:
                pod_status = "Deleted"
            else:
                pod_status = self.yaml["status"]["phase"]
            if "status" in self.yaml and "containerStatuses" in self.yaml["status"]:
                for container_status in self.yaml["status"]["containerStatuses"]:
                    if "state" in container_status:
                        if (
                            "waiting" in container_status["state"]
                            and "reason" in container_status["state"]["waiting"]
                            and "BackOff" in container_status["state"]["waiting"]["reason"]
                        ):
                            return "Failed"
                        if "error" in container_status["state"]:
                            return "Failed"

            return pod_status
        else:
            raise Exception("Not implemented for kind: " + self.kind)

    def update_pod_state(self, event_type, old_status):
        # Called to update a current pod state.
        # may start/read pod logs.
        cur_status = self.status
        need_read_pod_logs = cur_status and cur_status != "Pending" and not self._has_read_logs
        read_pod_static_logs = self.status != "Running"

        if need_read_pod_logs:
            self._log_reader = ThreadedKubernetesWatchPodLog()
            self._log_reader.pipe(self)
            self._has_read_logs = True
            if read_pod_static_logs:
                # in case where the logs were too fast,
                # and they need to be read sync.
                self._log_reader.read_currnet_logs(self.client, self.name, self.namespace)
                self._log_reader = None
                pass
            else:
                # async read logs.
                self._log_reader.start(self.client, self.name, self.namespace)

    def update_object_state(self, event_type, object_yaml: dict):
        is_new = self._object_yaml is None
        old_status = None if is_new else self.status

        # update the current yaml.
        self._object_yaml = object_yaml

        if not self._was_deleted:
            self._was_deleted = event_type == "DELETED"

        if self.kind == "Pod":
            self.update_pod_state(event_type, old_status)

        if old_status != self.status:
            self.emit("status", self.status, self)


class ThreadedKubernetesNamespaceObjectsWatcher(EventHandler):
    _object_watchers: Dict[str, ThreadedKubernetesObjectsWatcher] = None
    _namespace_watchers: Dict[str, List[ThreadedKubernetesWatch]] = None
    auto_watch_pod_logs: bool = True
    remove_deleted_kube_objects_from_memory: bool = True
    client: kubernetes.client.CoreV1Api = None

    def __init__(self, client: kubernetes.client.CoreV1Api):
        super().__init__()
        self.client = client
        self._object_watchers = dict()
        self._namespace_watchers = dict()

    @property
    def object_watchers(self):
        return self._object_watchers

    def watch_namespace(self, namespace: str, label_selector: str = None, field_selector: str = None):
        assert isinstance(namespace, str) and len(namespace) > 0
        if namespace in self._namespace_watchers:
            raise Exception("Namespace already being watched.")

        watchers = []
        self._namespace_watchers[namespace] = watchers
        for kind in ["Pod", "Job", "Deployment", "Service"]:
            watcher = ThreadedKubernetesWatchNamspeace()
            watcher.pipe(self)
            watcher.start(
                client=self.client,
                namespace=namespace,
                kind=kind,
                field_selector=field_selector,
                label_selector=label_selector,
            )
            watchers.append(watcher)

    def emit(self, name, *args, **kwargs):
        if name == "DELETED":
            self.delete_object(args[0])
        elif name == "update":
            self.update_object(args[0])

        if len(args) < 2:
            args = list(args) + [self]

        super().emit(name, *args, **kwargs)

    def update_object(self, event):
        kube_object = event["object"]
        event_type = event["type"]
        oid = ThreadedKubernetesObjectsWatcher.compose_object_id_from_yaml(kube_object)
        if oid not in self._object_watchers:
            if event_type == "DELETED":
                return
            self._object_watchers[oid] = ThreadedKubernetesObjectsWatcher(
                self.client, kube_object, self.auto_watch_pod_logs
            )
            self._object_watchers[oid].pipe(self)

        self._object_watchers[oid].update_object_state(event_type, kube_object)

    def delete_object(self, event):
        # first update the current object.
        self.update_object(event)

        kube_object = event["object"]
        oid = ThreadedKubernetesObjectsWatcher.compose_object_id_from_yaml(kube_object)
        if oid in self._object_watchers:
            watcher = self._object_watchers[oid]
            watcher.stop()
            if self.remove_deleted_kube_objects_from_memory:
                del self._object_watchers[oid]

    def waitfor(
        self, predict, include_log_events: bool = False, timeout: float = None, event_type: str = "update",
    ) -> ThreadedKubernetesObjectsWatcher:
        class wait_event:
            args: list = None
            kwargs: dict = None

            def __init__(self, args, kwargs):
                super().__init__()
                self.args = args
                self.kwargs = kwargs

        event_queue = SimpleQueue()

        def add_queue_event(*args, **kwargs):
            event_queue.put(wait_event(list(args), dict(kwargs)))

        event_handler_idx = self.on(event_type, add_queue_event)

        info = None
        while True:
            info = event_queue.get(timeout=timeout)
            args = info.args
            kwargs = info.kwargs
            if predict(*args, **kwargs):
                break

        self.clear(event_type, event_handler_idx)
        return None if info is None else info.args[-1]

    def waitfor_status(
        self,
        kind: str = None,
        name: str = None,
        namespace: str = None,
        status: str = None,
        status_list: List[str] = None,
        predict=None,
        timeout: float = None,
        check_past_events: bool = True,
    ) -> ThreadedKubernetesObjectsWatcher:
        assert status is not None or (status_list is not None and len(status_list) > 0) or predict is not None

        def default_predict(match_status: str, sender: ThreadedKubernetesObjectsWatcher):
            if status is not None and status == match_status:
                return True
            if status_list is not None:
                for sli in status_list:
                    if sli == match_status:
                        return True
            return False

        predict = predict or default_predict

        def wait_predict(status, sender: ThreadedKubernetesObjectsWatcher):
            if name is not None and sender.name != name:
                return False
            if namespace is not None and sender.namespace != namespace:
                return False
            if kind is not None and sender.kind != kind:
                return False
            return predict(status, sender)

        # check if need past events to be loaded.
        if check_past_events:
            for sender in self._object_watchers.values():
                if wait_predict(sender.status, sender):
                    return sender

        return self.waitfor(wait_predict, False, timeout=timeout, event_type="status")

    def stop(self):
        for namespace, watchers in self._namespace_watchers.items():
            for watcher in watchers:
                watcher.stop()

        for oid, owatch in self._object_watchers.items():
            owatch.stop()
