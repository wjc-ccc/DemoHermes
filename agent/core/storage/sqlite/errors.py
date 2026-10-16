"""调用方可稳定识别的存储错误。"""

class RevisionConflictError(ValueError):
    pass

class IntegrityError(ValueError):
    pass

class LeaseLostError(RuntimeError):
    pass

class AuditConflictError(IntegrityError):
    error_code = 'audit_conflict'

class SessionIntegrityError(IntegrityError):
    error_code = 'session_integrity_error'
