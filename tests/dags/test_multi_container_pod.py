from utils import default_args, name_from_file
from datetime import timedelta
from airflow import DAG
from airflow_kubernetes_job_operator.kubernetes_job_operator import KubernetesJobOperator

dag = DAG(
    name_from_file(__file__),
    default_args=default_args,
    description="Test base job operator",
    schedule_interval=None,
    catchup=False,
)

namespace = None
default_delete_policy = "IfSucceeded"

with dag:
    KubernetesJobOperator(
        task_id="two-containers",
        namespace=namespace,
        body_filepath="./templates/test_multi_container_pod.yaml",
        dag=dag,
        delete_policy=default_delete_policy,
    )


if __name__ == "__main__":
    dag.clear(reset_dag_runs=True)
    dag.run()
