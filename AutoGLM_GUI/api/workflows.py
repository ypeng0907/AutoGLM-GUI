"""Workflow API 路由."""

from typing import Any

from fastapi import APIRouter, HTTPException

from AutoGLM_GUI.schemas import (
    WorkflowCreate,
    WorkflowListResponse,
    WorkflowResponse,
    WorkflowRunRequest,
    WorkflowRunResponse,
    WorkflowUpdate,
)

router = APIRouter()


@router.get("/api/workflows", response_model=WorkflowListResponse)
def list_workflows() -> WorkflowListResponse:
    """获取所有 workflows."""
    from AutoGLM_GUI.workflow_manager import workflow_manager

    workflow_dicts = workflow_manager.list_workflows()
    workflows = [WorkflowResponse(**wf) for wf in workflow_dicts]
    return WorkflowListResponse(workflows=workflows)


@router.get("/api/workflows/{workflow_uuid}", response_model=WorkflowResponse)
def get_workflow(workflow_uuid: str) -> WorkflowResponse:
    """获取单个 workflow."""
    from AutoGLM_GUI.workflow_manager import workflow_manager

    workflow = workflow_manager.get_workflow(workflow_uuid)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowResponse(**workflow)


@router.post("/api/workflows", response_model=WorkflowResponse)
def create_workflow(request: WorkflowCreate) -> WorkflowResponse:
    """创建新 workflow."""
    from AutoGLM_GUI.workflow_manager import workflow_manager

    try:
        workflow = workflow_manager.create_workflow(
            name=request.name, text=request.text
        )
        return WorkflowResponse(**workflow)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/workflows/{workflow_uuid}", response_model=WorkflowResponse)
def update_workflow(workflow_uuid: str, request: WorkflowUpdate) -> WorkflowResponse:
    """更新 workflow."""
    from AutoGLM_GUI.workflow_manager import workflow_manager

    workflow = workflow_manager.update_workflow(
        uuid=workflow_uuid, name=request.name, text=request.text
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowResponse(**workflow)


@router.delete("/api/workflows/{workflow_uuid}")
def delete_workflow(workflow_uuid: str) -> dict[str, Any]:
    """删除 workflow."""
    from AutoGLM_GUI.workflow_manager import workflow_manager

    success = workflow_manager.delete_workflow(workflow_uuid)
    if not success:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"success": True, "message": "Workflow deleted"}


@router.post(
    "/api/workflows/{workflow_uuid}/run", response_model=WorkflowRunResponse
)
async def run_workflow(
    workflow_uuid: str, request: WorkflowRunRequest
) -> WorkflowRunResponse:
    """立即执行 workflow（手动触发）."""
    from AutoGLM_GUI.scheduler_manager import scheduler_manager
    from AutoGLM_GUI.workflow_manager import workflow_manager

    workflow = workflow_manager.get_workflow(workflow_uuid)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if not request.device_serialnos and not request.device_group_id:
        raise HTTPException(
            status_code=400,
            detail="Either device_serialnos or device_group_id is required",
        )

    result = await scheduler_manager.run_workflow_now(
        workflow_uuid=workflow_uuid,
        device_serialnos=request.device_serialnos,
        device_group_id=request.device_group_id,
        execution_mode=request.execution_mode,
    )
    return WorkflowRunResponse(**result)
