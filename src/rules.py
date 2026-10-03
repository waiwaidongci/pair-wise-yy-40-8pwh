from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='建筑抗震鉴定与加固排序'; ENTITY='抗震鉴定'; ID_PREFIX='SR'
SEVERITIES=['low', 'medium', 'high', 'severe']; STATES=['proposed', 'assessed', 'design', 'construction', 'accepted', 'rejected']; TRANSITIONS={'proposed': ['assessed'], 'assessed': ['design', 'rejected'], 'design': ['construction'], 'construction': ['accepted'], 'accepted': ['rejected'], 'rejected': []}; TRANSITION_ROLES={'assessed': ['assessor'], 'design': ['structural_engineer'], 'construction': ['structural_engineer'], 'accepted': ['review_board'], 'rejected': ['review_board']}
CREATE_ROLES=set(['assessor']); RECORD_ROLES=set(['assessor', 'structural_engineer']); AUDIT_ROLES=set(['review_board', 'viewer']); VIEW_ROLES=set(['assessor', 'structural_engineer', 'review_board', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'severe': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'severe': 4}; TERMINAL_STATES=set(['accepted', 'rejected'])
BATCH_PREFIX='RB'
BATCH_DRAFT='draft'; BATCH_SUBMITTED='submitted'; BATCH_FAILED='failed'; BATCH_STATES=(BATCH_DRAFT,BATCH_SUBMITTED,BATCH_FAILED)
ENTRY_PENDING='pending'; ENTRY_REPORTED='reported'; ENTRY_MISMATCH='mismatch'; ENTRY_MISSING='missing'
REPORT_CONCLUSION_FIELDS=('status','severity','priority','quantity','threshold','deadline_hours','escalation_required')
BATCH_CREATE_ROLES=set(['assessor','structural_engineer']); BATCH_CONFIRM_ROLES=set(['review_board']); BATCH_VIEW_ROLES=set(['assessor','structural_engineer','review_board','viewer'])
MAX_BATCH_ITEMS=200
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def report_conclusion(item,open_records=0):
    return {'status':item['status'],'severity':item['severity'],'priority':priority_score(item['severity'],item['quantity'],item['threshold'],open_records),'quantity':item['quantity'],'threshold':item['threshold'],'deadline_hours':response_deadline_hours(item['severity'],item['quantity'],item['threshold']),'escalation_required':escalation_required(item['severity'],item['quantity'],item['threshold'])}
def conclusion_diff(local,ledger):
    diffs=[]
    for field in REPORT_CONCLUSION_FIELDS:
        lv=local.get(field); rv=(ledger or {}).get(field)
        if lv!=rv: diffs.append({'field':field,'local':lv,'ledger':rv})
    return diffs
