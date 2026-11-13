from pydantic import BaseModel


class InspectRequest(BaseModel):
    dataset_id: int


class RuleEnablePatch(BaseModel):
    enabled: bool


class DriftGuardConfigPatch(BaseModel):
    dataset_id: int | None = None
    enabled: bool | None = None
    strategy: str | None = None
    compare_fields: list[str] | None = None
