from app.services.resources import ResourceService
from app.services.workspace import WorkspaceService
from app.services.object_storage import ObjectStorageService


def test_model_path_does_not_falsely_advertise_unimplemented_inference(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "not_a_real_checkpoint.pt"))
    workspace = WorkspaceService(tmp_path)
    service = ResourceService(workspace, ObjectStorageService(workspace=workspace))
    assert service.discover("user", "thread").available_scientific_models == []
