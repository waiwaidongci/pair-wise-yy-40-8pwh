from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"; REPORT_FAILED="report_failed"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
class ReportFailed(ConflictError):
    """上报监管台账失败，批次已落库为 failed，可按同一批次号重试。"""
    kind=ErrorKind.REPORT_FAILED
    def __init__(self,message,batch_id):
        super().__init__(message); self.batch_id=batch_id
SEVERITIES=['low', 'medium', 'high', 'severe']; STATES=['proposed', 'assessed', 'design', 'construction', 'accepted', 'rejected']; ROLES=['assessor', 'structural_engineer', 'review_board', 'viewer']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
@dataclass(frozen=True)
class ReportBatch:
    id:int; batch_no:str; status:str; created_by:str; confirmed_by:Optional[str]; receipt_no:Optional[str]; diff:list; last_error:Optional[str]; created_at:str; confirmed_at:Optional[str]
@dataclass(frozen=True)
class ReportBatchItem:
    id:int; batch_id:int; item_id:int; external_ref:str; snapshot:Dict[str,Any]; local_version:int; receipt_version:Optional[int]; receipt_status:str; diff:list; stale:bool; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
