import yaml
import copy
import logging
import os

from logging import Logger
from uuid import uuid4
from typing import Callable, List, Type, Union, Set
from airflow_kubernetes_job_operator.kube_api.utils import not_empty_string
from airflow_kubernetes_job_operator.utils import randomString
from airflow_kubernetes_job_operator.collections import JobRunnerDeletePolicy, JobRunnerException
from airflow_kubernetes_job_operator.kube_api import (
    KubeApiConfiguration,
    GetAPIVersions,
    KubeApiRestQuery,
    KubeApiRestClient,
    KubeResourceKind,
    KubeResourceDescriptor,
    NamespaceWatchQuery,
    DeleteNamespaceObject,
    CreateNamespaceObject,
    ConfigureNamespaceObject,
    KubeResourceState,
    GetNamespaceObjects,
    kube_logger,
)


class JobRunner:
    job_runner_instance_id_label_name = "kubernetes-job-runner-instance-id"
    custom_prepare_kinds: dict = {}
    show_runner_id_on_logs: bool = None  # type:ignore

    def __init__(
        self,
        body,
        namespace: str = None,
        logger: Logger = kube_logger,
        show_pod_logs: bool = True,
        show_operation_logs: bool = True,
        show_watcher_logs: bool = True,
        show_executor_logs: bool = True,
        show_error_logs: bool = True,
        delete_policy: JobRunnerDeletePolicy = JobRunnerDeletePolicy.IfSucceeded,
        auto_load_kube_config=True,
        random_name_postfix_length=8,
        name_prefix: str = None,
        name_postfix: str = None,
    ):
        assert isinstance(body, (list, dict, str)), ValueError(
            "body must be a dictionary, a list of dictionaries with at least one value, or string yaml"
        )

        self._client = KubeApiRestClient(auto_load_kube_config=auto_load_kube_config)
        self._is_body_ready = False
        self._body = body
        self.logger: Logger = logger
        self._id = str(uuid4())

        self.name_postfix = (
            name_postfix or "" if random_name_postfix_length <= 0 else randomString((random_name_postfix_length))
        )
        self.name_prefix = name_prefix

        self.namespace = namespace
        self.show_pod_logs = show_pod_logs
        self.show_operation_logs = show_operation_logs
        self.show_watcher_logs = show_watcher_logs
        self.show_executor_logs = show_executor_logs
        self.show_error_logs = show_error_logs
        self.delete_policy = delete_policy

        if self.show_runner_id_on_logs is None:
            self.show_runner_id_on_logs = (
                os.environ.get("KUBERNETES_JOB_OPERATOR_SHOW_RUNNER_ID_IN_LOGS", "false").lower() == "true"
            )

        super().__init__()

    @property
    def id(self) -> str:
        return self._id

    @property
    def body(self) -> List[dict]:
        if not self._is_body_ready:
            self.prepare_body()
        return self._body

    @property
    def client(self) -> KubeApiRestClient:
        return self._client

    @property
    def job_label_selector(self) -> str:
        return f"{self.job_runner_instance_id_label_name}={self.id}"

    @classmethod
    def register_custom_prepare_kind(cls, kind: Union[KubeResourceKind, str], preapre_kind: Callable):
        if isinstance(kind, str):
            kind = KubeResourceKind.get_kind(kind)

        assert isinstance(kind, KubeResourceKind), ValueError("kind must be an instance of KubeResourceKind or string")
        cls.custom_prepare_kinds[kind.name] = preapre_kind

    @classmethod
    def update_metadata_labels(cls, body: dict, labels: dict):
        assert isinstance(body, dict), ValueError("Body must be a dictionary")
        assert isinstance(labels, dict), ValueError("labels must be a dictionary")

        if isinstance(body.get("spec", None), dict) or isinstance(body.get("metadata", None), dict):
            body.setdefault("metadata", {})
            metadata = body["metadata"]
            if "labels" not in metadata:
                metadata["labels"] = copy.deepcopy(labels)
            else:
                metadata["labels"].update(labels)
        for o in body.values():
            if isinstance(o, dict):
                cls.update_metadata_labels(o, labels)

    @classmethod
    def custom_prepare_job_kind(cls, body: dict):
        assert isinstance(body, dict), ValueError("Body must be a dictionary")
        descriptor = KubeResourceDescriptor(body)

        assert isinstance(descriptor.spec.get("template", None), dict), JobRunnerException(
            "Cannot create a job without a template, 'spec.template' is missing or not a dictionary"
        )

        assert isinstance(descriptor.metadata["template"].get("spec", None), dict), JobRunnerException(
            "Cannot create a job without a template spec, 'spec.template.spec' is missing or not a dictionary"
        )

        descriptor.spec.setdefault("backoffLimit", 0)
        descriptor.metadata.setdefault("finalizers", [])
        descriptor.spec["template"]["spec"].setdefault("restartPolicy", "Never")

        if "foregroundDeletion" not in descriptor.metadata["finalizers"]:
            descriptor.metadata["finalizers"].append("foregroundDeletion")

    @classmethod
    def custom_prepare_pod_kind(cls, body: dict):
        assert isinstance(body, dict), ValueError("Body must be a dictionary")
        descriptor = KubeResourceDescriptor(body)
        descriptor.spec.setdefault("restartPolicy", "Never")

    def prepare_body(self, force=False):
        if not force and self._is_body_ready:
            return

        body = self._body
        if isinstance(body, str):
            body = list(yaml.safe_load_all(body))
        else:
            body = copy.deepcopy(body)
            if not isinstance(body, list):
                body = [body]

        assert all(isinstance(o, dict) for o in body), JobRunnerException(
            "Failed to parse body, found errored collection values"
        )

        for obj in body:
            self._prepare_kube_object(obj)
        self._body = body
        self._is_body_ready = True

    def _prepare_kube_object(
        self,
        body: dict,
    ):
        assert isinstance(body, dict), ValueError("Body but be a dictionary")

        kind_name: str = body.get("kind", None)
        if kind_name is None or not KubeResourceKind.has_kind(kind_name.strip().lower()):
            raise JobRunnerException(
                f"Unrecognized kubernetes object kind: '{kind_name}', "
                + f"Allowed core kinds are {KubeResourceKind.all_names()}. "
                + "To register new kinds use: KubeResourceKind.register_global_kind(..), "
                + "more information cab be found @ "
                + "https://github.com/LamaAni/KubernetesJobOperator/docs/add_custom_kinds.md",
            )

        descriptor = KubeResourceDescriptor(body)
        assert descriptor.spec is not None, ValueError("body['spec'] is not defined")

        descriptor.metadata.setdefault("namespace", self.namespace or self.client.get_default_namespace())
        name_parts = [v for v in [self.name_prefix, descriptor.name, self.name_postfix] if not_empty_string(v)]
        assert len(name_parts) > 0, JobRunnerException("Invalid name or auto generated name")
        descriptor.name = "-".join(name_parts)

        self.update_metadata_labels(
            body,
            {
                self.job_runner_instance_id_label_name: self.id,
            },
        )

        if kind_name in self.custom_prepare_kinds:
            self.custom_prepare_kinds[kind_name](body)

        return body

    def _create_body_operation_queries(self, operator: Type[ConfigureNamespaceObject]) -> List[KubeApiRestQuery]:
        queries = []
        for obj in self.body:
            q = operator(obj)
            queries.append(q)
            if self.show_operation_logs:
                q.pipe_to_logger(self.logger)
        return queries

    def log(self, *args, level=logging.INFO):
        marker = f"job-runner-{self.id}" if self.show_runner_id_on_logs else "job-runner"
        if self.show_executor_logs:
            self.logger.log(level, f"{{{marker}}}: {args[0] if len(args)>0 else ''}", *args[1:])

    def execute_job(
        self,
        timeout: int = 60 * 5,
        watcher_start_timeout: int = 10,
    ):
        self.prepare_body()

        # prepare the run objects.
        namespaces: List[str] = []
        all_kinds: List[KubeResourceKind] = list(KubeResourceKind.watchable())
        descriptors = [KubeResourceDescriptor(r) for r in self.body]

        assert len(descriptors) > 0, JobRunnerException("You must have at least one resource to execute.")

        state_object = descriptors[0]

        assert all(d.kind is not None for d in descriptors), JobRunnerException(
            "All resources in execution must have a recognizable object kind: (resource['kind'] is not None)"
        )

        assert state_object.kind.parse_kind_state is not None, JobRunnerException(
            "The first object in the object list must have a kind with a parseable state, "
            + "where the states Failed or Succeeded are returned when the object finishes execution."
            + f"Active kinds with parseable states are: {[k.name for k in KubeResourceKind.parseable()]}. "
            + "To register new kinds or add a parse_kind_state use "
            + "KubeResourceKind.register_global_kind(..) and associated methods. "
            + "more information cab be found @ "
            + "https://github.com/LamaAni/KubernetesJobOperator/docs/add_custom_kinds.md",
        )

        for resource in descriptors:
            descriptor = KubeResourceDescriptor(resource)
            assert descriptor.kind is not None, JobRunnerException("Cannot execute an object without a kind")

            all_kinds.append(descriptor.kind)
            if descriptor.namespace is not None:
                namespaces.append(descriptor.namespace)

        namespaces = list(set(namespaces))
        all_kinds = list(set(all_kinds))

        # scan the api and get all current watchable kinds.
        watchable_kinds = GetAPIVersions.get_existing_api_kinds(self.client, all_kinds)

        assert state_object.kind in watchable_kinds, JobRunnerException(
            "The first object in the collection must be watchable and createable, to allow the runner to "
            + f"properly execute. The kind {str(state_object.kind)} was not found in the api."
        )

        for kind in KubeResourceKind.watchable():
            if kind not in watchable_kinds:
                self.log(
                    f"Could not find kind '{kind}' in the api server. This kind is not watched and events will not be logged",
                    level=logging.WARNING,
                )

        context_info = KubeApiConfiguration.get_active_context_info(self.client.kube_config)
        self.log(f"Executing context: {context_info.get('name','unknown')}")
        self.log(f"Executing cluster: {context_info.get('context',{}).get('cluster')}")

        # create the watcher
        watcher = NamespaceWatchQuery(
            kinds=watchable_kinds,
            namespace=list(set(namespaces)),  # type:ignore
            timeout=timeout,
            watch_pod_logs=self.show_pod_logs,
            label_selector=self.job_label_selector,
        )

        # start the watcher
        if self.show_watcher_logs:
            watcher.pipe_to_logger(self.logger)

        self.client.query_async([watcher])
        try:
            watcher.wait_until_running(timeout=watcher_start_timeout)
        except TimeoutError as ex:
            raise JobRunnerException(
                "Failed while waiting for watcher to start. Unable to connect to cluster.",
                ex,
            )

        self.log(f"Started watcher for kinds: {', '.join(list(watcher.kinds.keys()))}")
        self.log(f"Watching namespaces: {', '.join(namespaces)}")

        # Creating the objects to run
        run_query_handler = self.client.query_async(self._create_body_operation_queries(CreateNamespaceObject))
        # binding the errors.
        run_query_handler.on(run_query_handler.error_event_name, lambda sender, err: watcher.emit_error(err))

        self.log(f"Waiting for {state_object.namespace}/{state_object.name} to finish...")

        try:
            final_state = watcher.wait_for_state(
                [KubeResourceState.Failed, KubeResourceState.Succeeded, KubeResourceState.Deleted],  # type:ignore
                kind=state_object.kind,
                name=state_object.name,
                namespace=state_object.namespace,
                timeout=timeout,
            )
        except Exception as ex:
            self.log("Execution timeout... deleting resources", level=logging.ERROR)
            self.abort()
            raise ex

        if final_state == KubeResourceState.Deleted:
            self.log(f"Failed to execute. Main resource {state_object} was delete", level=logging.ERROR)
            self.abort()
            raise JobRunnerException("Resource was deleted while execution was running, execution failed.")

        self.log(f"Job {final_state}")

        if final_state == KubeResourceState.Failed and self.show_error_logs:
            # print the object states.
            kinds = [o.kind.name for o in watcher.watched_objects]
            queries: List[GetNamespaceObjects] = []
            for namespace in namespaces:
                for kind in set(kinds):
                    queries.append(GetNamespaceObjects(kind, namespace, label_selector=self.job_label_selector))
            self.log("Reading result error (status) objects..")
            resources = [KubeResourceDescriptor(o) for o in self.client.query(queries)]
            self.log(
                f"Found {len(resources)} resources related to this run:\n"
                + "\n".join(f" - {str(r)}" for r in resources)
            )
            for resource in resources:
                obj_state: KubeResourceState = KubeResourceState.Active
                if resource.kind is not None:
                    obj_state = resource.kind.parse_state(resource.body)
                if resource.status is not None:
                    self.logger.error(f"[{resource}]: {obj_state}, status:\n" + yaml.safe_dump(resource.status))
                else:
                    self.logger.error(f"[{resource}]: Has {obj_state} (status not provided)")

        if (
            self.delete_policy == JobRunnerDeletePolicy.Always
            or (self.delete_policy == JobRunnerDeletePolicy.IfFailed and final_state == KubeResourceState.Failed)
            or (self.delete_policy == JobRunnerDeletePolicy.IfSucceeded and final_state == KubeResourceState.Succeeded)
        ):
            self.log(f"Deleting resources due to policy: {str(self.delete_policy)}")
            self.delete_job()

        self.client.stop()
        self.log("Client stopped, execution completed.")

        return final_state

    def delete_job(self):
        """Using a pre-prepared job yaml, deletes a currently executing job.

        Arguments:

            body {dict} -- The job description yaml.
        """
        self.log(("Deleting job.."))
        descriptors: List[KubeResourceDescriptor] = [KubeResourceDescriptor(o) for o in self.body]
        descriptors = [d for d in descriptors if d.kind is not None and d.name is not None and d.namespace is not None]

        self.log(
            "Deleting objects: " + ", ".join([f"{d}" for d in descriptors]),
        )
        self.client.query(self._create_body_operation_queries(DeleteNamespaceObject))
        self.log("Job deleted")

    def abort(self):
        self.log("Aborting job ...")
        self.delete_job()
        self.client.stop()
        self.log("Client stopped, execution aborted.")


JobRunner.register_custom_prepare_kind("pod", JobRunner.custom_prepare_pod_kind)
JobRunner.register_custom_prepare_kind("job", JobRunner.custom_prepare_job_kind)
