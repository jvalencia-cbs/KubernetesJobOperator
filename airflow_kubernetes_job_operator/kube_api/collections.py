import re
from enum import Enum
from typing import Callable, List


def not_empty_string(val: str):
    """Returns true if the string is not empty (len>0) and not None """
    return isinstance(val, str) and len(val) > 0


class KubeResourceState(Enum):
    """Represents the state of an resource.

    Args:
        Enum ([type]): [description]

    Returns:
        [type]: [description]
    """

    Pending = "Pending"
    Active = "Active"
    Succeeded = "Succeeded"
    Failed = "Failed"
    Running = "Running"
    Deleted = "Deleted"

    def __str__(self) -> str:
        return self.value

    def __repr__(self):
        return str(self)


class KubeApiRestQueryConnectionState(Enum):
    Disconnected = "Disconnected"
    Connecting = "Connecting"
    Streaming = "Streaming"

    def __str__(self) -> str:
        return self.value


def parse_kind_state_default(yaml: dict) -> "KubeResourceState":
    return KubeResourceState.Active


global kinds_collection

kinds_collection = {}


class KubeResourceKind:
    def __init__(
        self,
        name: str,
        api_version: str,
        parse_kind_state: Callable = None,
        auto_include_in_watch: bool = True,
    ):
        """Represents a kubernetes resource kind.

        Args:
            name (str): The kind name (Pod, Job, Service ...)
            api_version (str): The resource api version.
            parse_kind_state (Callable, optional): A method, lambda yaml: object -> KubeResourceState. If exists
            will be used to parse the state of the object. Defaults to None.
            auto_include_in_watch (bool, optional): When a watcher is called, should this object be included.
                Defaults to True.
        """
        super().__init__()

        assert isinstance(name, str) and len(name.strip()) > 0, ValueError("Invalid kind name: " + name)
        assert isinstance(api_version, str) and len(api_version.strip()) > 0, ValueError(
            "Invalid kind api_version: " + api_version
        )
        assert parse_kind_state is None or isinstance(
            parse_kind_state, Callable
        ), "parse_kind_state must be None or a callable"

        self._name = name.lower()
        self.parse_kind_state = parse_kind_state
        self.api_version = api_version
        self.auto_include_in_watch = auto_include_in_watch

    @property
    def name(self) -> str:
        return self._name

    @property
    def plural(self) -> str:
        return self.name + "s"

    def parse_state(self, body: dict, was_deleted: bool = False) -> KubeResourceState:
        """Parses the state of the kind given the object body.

        Args:
            body (dict): Returns the kind state.
            was_deleted (bool, optional): If true, will return KubeResourceState.Deleted. Defaults to False.

        Returns:
            KubeResourceState: The state of the current object.
        """
        if was_deleted:
            return KubeResourceState.Deleted
        else:
            state = (self.parse_kind_state or parse_kind_state_default)(body)
            if not isinstance(state, KubeResourceState):
                state = KubeResourceState(state)
            return state

    def compose_resource_path(
        self,
        namespace: str,
        name: str = None,
        api_version: str = None,
        suffix: str = None,
    ) -> str:
        """Create a resource path from the kind.

        Args:
            namespace (str): The kind namespace to add.
            name (str, optional): The resource name to add. Defaults to None.
            api_version (str, optional): Override the kind api_version. Defaults to None.
            suffix (str, optional): The additional resource suffix (like 'logs'). Defaults to None.

        Returns:
            str: The resource path.
        """
        api_version = api_version or self.api_version
        version_header = "apis"
        if re.match(r"v[0-9]+", api_version):
            version_header = "api"
        composed = [
            version_header,
            api_version,
            "namespaces",
            namespace,
            self.plural,
            name,
            suffix,
        ]
        resource_path = ("/".join([v for v in composed if v is not None])).strip()
        if not resource_path.startswith("/"):
            resource_path = "/" + resource_path
        return resource_path

    @classmethod
    def create_from_existing(cls, name: str, api_version: str = None, parse_kind_state: Callable = None):
        global kinds_collection
        assert not_empty_string(name), ValueError("name cannot be null")
        name = name.lower()
        if name.lower() not in kinds_collection:
            return KubeResourceKind(name, api_version, parse_kind_state)
        global_kind = cls.get_kind(name)

        return KubeResourceKind(
            name,
            api_version or global_kind.api_version,
            parse_kind_state or global_kind.parse_kind_state,
        )

    @classmethod
    def has_kind(cls, name: str) -> bool:
        global kinds_collection
        return name in kinds_collection

    @classmethod
    def get_kind(cls, name: str) -> "KubeResourceKind":
        global kinds_collection
        assert isinstance(name, str) and len(name) > 0, ValueError("Kind must be a non empty string")
        name = name.lower()
        assert name in kinds_collection, ValueError(
            f"Unknown kubernetes object kind: {name},"
            + " you can use KubeResourceKind.register_global_kind to add new ones."
            + " (airflow_kubernetes_job_operator.kube_api.KubeResourceKind)"
        )
        return kinds_collection[name]

    @classmethod
    def all(cls) -> List["KubeResourceKind"]:
        global kinds_collection
        return kinds_collection.values()

    @classmethod
    def parseable(cls) -> List["KubeResourceKind"]:
        """Returns all parseable kinds (i.e. have parse_kind_state not None)"""
        return [k for k in cls.all() if k.parse_kind_state is not None]

    @classmethod
    def watchable(cls) -> List["KubeResourceKind"]:
        """Returns all the kinds that have auto_include_in_watch as true"""
        return [k for k in cls.all() if k.auto_include_in_watch is True]

    @classmethod
    def all_names(cls) -> List[str]:
        global kinds_collection
        return kinds_collection.keys()

    @classmethod
    def register_global_kind(cls, kind: "KubeResourceKind"):
        global kinds_collection
        kinds_collection[kind.name] = kind

    @staticmethod
    def parse_state_job(yaml: dict) -> KubeResourceState:
        status = yaml.get("status", {})
        spec = yaml.get("spec", {})
        back_off_limit = int(spec.get("backoffLimit", 0))

        job_status = KubeResourceState.Pending
        if "failed" in status and int(status.get("failed", 0)) > back_off_limit:
            job_status = KubeResourceState.Failed
        elif "startTime" in status:
            if "completionTime" in status:
                job_status = KubeResourceState.Succeeded
            else:
                job_status = KubeResourceState.Running

        return job_status

    @staticmethod
    def parse_state_pod(yaml: dict) -> KubeResourceState:

        status = yaml.get("status", {})
        pod_phase = status["phase"]
        container_status = status.get("containerStatuses", [])

        for container_status in container_status:
            if "state" in container_status:
                if (
                    "waiting" in container_status["state"]
                    and "reason" in container_status["state"]["waiting"]
                    and "BackOff" in container_status["state"]["waiting"]["reason"]
                ):
                    return KubeResourceState.Failed
                if "error" in container_status["state"]:
                    return KubeResourceState.Failed

        if pod_phase == "Pending":
            return KubeResourceState.Pending
        elif pod_phase == "Running":
            return KubeResourceState.Running
        elif pod_phase == "Succeeded":
            return KubeResourceState.Succeeded
        elif pod_phase == "Failed":
            return KubeResourceState.Failed
        return pod_phase

    def __eq__(self, o: "KubeResourceKind") -> bool:
        if not isinstance(o, KubeResourceKind):
            return False
        return o.api_version == self.api_version and o.name == self.name

    def __str__(self) -> str:
        return f"{self.api_version}/{self.plural}"


for kind in [
    KubeResourceKind(api_version="v1", name="Pod", parse_kind_state=KubeResourceKind.parse_state_pod),
    KubeResourceKind(api_version="v1", name="Service"),
    KubeResourceKind(api_version="v1", name="Event", auto_include_in_watch=False),
    KubeResourceKind(api_version="batch/v1", name="Job", parse_kind_state=KubeResourceKind.parse_state_job),
    KubeResourceKind(api_version="apps/v1", name="Deployment"),
]:
    KubeResourceKind.register_global_kind(kind)


class KubeResourceDescriptor:
    def __init__(
        self,
        body: dict,
        api_version: str = None,
        namespace: str = None,
        name: str = None,
        assert_metadata: bool = True,
    ):
        super().__init__()
        assert isinstance(body, dict), ValueError("Error while parsing resource: body must be a dictionary", body)

        self._body = body
        self._kind = (
            None
            if self.body.get("kind", None) is None
            else KubeResourceKind.create_from_existing(
                self.body.get("kind"),
                api_version or self.body.get("apiVersion"),
            )
        )

        if assert_metadata:
            if "metadata" not in self.body:
                self.body["metadata"] = {}

        if namespace or self.namespace:
            self.metadata["namespace"] = namespace or self.namespace
        if name or self.name:
            self.metadata["name"] = name or self.name

    @property
    def self_link(self) -> str:
        return self.metadata["self-link"]

    @property
    def body(self) -> dict:
        return self._body

    @property
    def kind(self) -> KubeResourceKind:
        return self._kind

    @property
    def kind_name(self) -> str:
        if self.kind is None:
            return self.body.get("kind", "{unknown}")
        return self.kind.name

    @property
    def kind_plural(self) -> object:
        return self.kind_name.lower() + "s" if self.kind is not None else None

    @property
    def spec(self) -> dict:
        return self.body.get("spec")

    @property
    def status(self) -> dict:
        return self.body.get("status")

    @property
    def state(self) -> KubeResourceState:
        return self.kind.parse_state(self.body, False)

    @property
    def name(self) -> str:
        return self.metadata.get("name", None)

    @name.setter
    def name(self, val: str):
        self.metadata["name"] = val

    @property
    def namespace(self) -> str:
        return self.metadata.get("namespace", None)

    @namespace.setter
    def namespace(self, val: str):
        self.metadata["namespace"] = val

    @property
    def metadata(self) -> dict:
        return self.body["metadata"]

    @property
    def api_version(self) -> str:
        return self.kind.api_version

    def __str__(self):
        if self.namespace is not None:
            if self.name:
                return f"{self.namespace}/{self.kind_plural}/{self.name}"
            else:
                return f"{self.namespace}/{self.kind_plural}/{self.name}"
        else:
            return f"{self.api_version}/{self.kind_name}"
