def _inject_otel_resource_attributes(resource: dict, metadata: dict) -> dict:
    enriched = dict(resource)
    enriched.update(
        {
            "ergon.task.name": metadata["task_name"],
            "ergon.task.execution.id": metadata["execution_id"],
            "ergon.task.execution.pid": metadata["pid"],
            "ergon.task.execution.host.name": metadata["host_name"],
            "ergon.task.execution.host.ip": metadata["host_ip"],
            "ergon.task.execution.start_time": metadata["execution_start_time"],
        }
    )
    return enriched
