import os
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from bson import ObjectId
from datetime import datetime, timezone

from database import db

app = FastAPI(title="Justifi API (MVP)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Utility -------------------------------------------------
class PyObjectId(ObjectId):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if isinstance(v, ObjectId):
            return v
        try:
            return ObjectId(str(v))
        except Exception:
            raise ValueError("Invalid ObjectId")


def serialize_id(doc: Dict[str, Any]):
    if not doc:
        return doc
    doc = dict(doc)
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    # Convert datetime objects
    for k, v in list(doc.items()):
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


# Schemas -------------------------------------------------
class JustificationCreate(BaseModel):
    title: str
    type_code: str
    department: str
    cost_centre: str
    requester_email: str
    urgency: str
    description: str
    business_impact: Optional[str] = None
    alternatives: Optional[str] = None
    cost_estimate: Optional[float] = None
    required_date: Optional[str] = None
    dynamic_values: Dict[str, Any] = Field(default_factory=dict)
    attachments: List[Dict[str, Any]] = Field(default_factory=list)


class CommentCreate(BaseModel):
    author_email: str
    message: str
    is_internal: bool = False


class ApproverAction(BaseModel):
    actor_email: str
    comment: Optional[str] = None


class RequestInfoAction(BaseModel):
    actor_email: str
    reason: str


class ResubmitPayload(BaseModel):
    actor_email: str
    message: Optional[str] = None


# Routing selection --------------------------------------

def select_routing_rule(department: str, type_code: str, spend: Optional[float]) -> Optional[Dict[str, Any]]:
    candidates = []
    if department and type_code:
        candidates.append({"department": department, "type_code": type_code})
    if type_code:
        candidates.append({"type_code": type_code})
    if department:
        candidates.append({"department": department})
    candidates.append({})

    for c in candidates:
        cur = db["routingrule"].find(c)
        for rule in cur:
            thr = rule.get("spend_threshold")
            if thr is None or (spend or 0) >= float(thr):
                return rule
    return None


# Audit helper -------------------------------------------

def log_audit(entity: str, entity_id: str, action: str, actor: str, details: Dict[str, Any] = None):
    doc = {
        "entity": entity,
        "entity_id": entity_id,
        "action": action,
        "actor_email": actor,
        "details": details or {},
        "timestamp": datetime.now(timezone.utc),
    }
    db["auditlog"].insert_one(doc)


# Email stub ---------------------------------------------

def send_email_stub(to: List[str], subject: str, html: str):
    db["emailoutbox"].insert_one({
        "to": to,
        "subject": subject,
        "html": html,
        "created_at": datetime.now(timezone.utc),
    })


# Routes --------------------------------------------------
@app.get("/")
def root():
    return {"name": "Justifi API", "status": "ok"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Connected & Working"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = os.getenv("DATABASE_NAME") or "❌ Not Set"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:20]
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:80]}"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response


@app.post("/api/justifications")
def create_justification(payload: JustificationCreate):
    data = payload.model_dump()
    data.update({
        "status": "PendingApproval",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })
    jid = db["justification"].insert_one(data).inserted_id
    jid_str = str(jid)

    # Routing
    rule = select_routing_rule(payload.department, payload.type_code, payload.cost_estimate)
    approvers: List[str] = []
    if rule and rule.get("approver_emails"):
        approvers = [a for a in rule["approver_emails"] if a]

    # Create approval tasks sequentially
    for idx, email in enumerate(approvers):
        db["approvaltask"].insert_one({
            "justification_id": jid_str,
            "approver_email": email,
            "step_index": idx,
            "status": "Pending",
            "created_at": datetime.now(timezone.utc),
        })

    # Emails
    if approvers:
        send_email_stub(approvers, "New approval request", f"Justification {payload.title} requires your approval.")
    send_email_stub([payload.requester_email], "Submission received", f"Your justification '{payload.title}' has been submitted.")

    log_audit("justification", jid_str, "CREATE", payload.requester_email, {"title": payload.title})

    return {"id": jid_str}


@app.get("/api/justifications")
def list_justifications(requester_email: Optional[str] = None, status: Optional[str] = None):
    q: Dict[str, Any] = {}
    if requester_email:
        q["requester_email"] = requester_email
    if status:
        q["status"] = status
    items = [serialize_id(d) for d in db["justification"].find(q).sort("created_at", -1)]
    return items


@app.get("/api/justifications/{jid}")
def get_justification(jid: str):
    doc = db["justification"].find_one({"_id": ObjectId(jid)})
    if not doc:
        raise HTTPException(404, "Not found")
    just = serialize_id(doc)
    tasks = [serialize_id(t) for t in db["approvaltask"].find({"justification_id": jid}).sort("step_index", 1)]
    comments = [serialize_id(c) for c in db["comment"].find({"justification_id": jid}).sort("created_at", 1)]
    audits = [serialize_id(a) for a in db["auditlog"].find({"entity": "justification", "entity_id": jid}).sort("timestamp", 1)]
    just.update({"approval_tasks": tasks, "comments": comments, "audit": audits})
    return just


@app.get("/api/inbox")
def approver_inbox(approver_email: str):
    tasks = list(db["approvaltask"].find({"approver_email": approver_email, "status": {"$in": ["Pending", "NeedsMoreInfo"]}}).sort("created_at", -1))
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        grouped.setdefault(t["justification_id"], []).append(t)
    result = []
    for jid, arr in grouped.items():
        next_task = sorted(arr, key=lambda x: x.get("step_index", 0))[0]
        just = db["justification"].find_one({"_id": ObjectId(jid)})
        result.append({
            "task": serialize_id(next_task),
            "justification": serialize_id(just) if just else None,
        })
    return result


@app.post("/api/approvals/{task_id}/approve")
def approve_task(task_id: str, action: ApproverAction):
    task = db["approvaltask"].find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(404, "Task not found")
    jid = task["justification_id"]

    db["approvaltask"].update_one({"_id": task["_id"]}, {"$set": {"status": "Approved", "updated_at": datetime.now(timezone.utc)}})
    log_audit("justification", jid, "APPROVE", action.actor_email, {"task_id": task_id, "comment": action.comment})

    # Check if all steps approved
    remaining = db["approvaltask"].count_documents({"justification_id": jid, "status": {"$ne": "Approved"}})
    if remaining == 0:
        db["justification"].update_one({"_id": ObjectId(jid)}, {"$set": {"status": "Approved", "updated_at": datetime.now(timezone.utc)}})
        just = db["justification"].find_one({"_id": ObjectId(jid)})
        send_email_stub([just.get("requester_email")], "Final approval", f"Your justification '{just.get('title')}' is approved.")
    return {"ok": True}


@app.post("/api/approvals/{task_id}/reject")
def reject_task(task_id: str, action: ApproverAction):
    if not action.comment:
        raise HTTPException(400, "Rejection requires a reason in comment")
    task = db["approvaltask"].find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(404, "Task not found")
    jid = task["justification_id"]

    db["approvaltask"].update_one({"_id": task["_id"]}, {"$set": {"status": "Rejected", "updated_at": datetime.now(timezone.utc)}})
    db["justification"].update_one({"_id": ObjectId(jid)}, {"$set": {"status": "Rejected", "updated_at": datetime.now(timezone.utc)}})
    log_audit("justification", jid, "REJECT", action.actor_email, {"task_id": task_id, "comment": action.comment})

    just = db["justification"].find_one({"_id": ObjectId(jid)})
    send_email_stub([just.get("requester_email")], "Rejected", f"Your justification '{just.get('title')}' was rejected. Reason: {action.comment}")
    return {"ok": True}


@app.post("/api/approvals/{task_id}/request-info")
def request_info(task_id: str, action: RequestInfoAction):
    task = db["approvaltask"].find_one({"_id": ObjectId(task_id)})
    if not task:
        raise HTTPException(404, "Task not found")
    jid = task["justification_id"]
    db["approvaltask"].update_one({"_id": task["_id"]}, {"$set": {"status": "NeedsMoreInfo", "requested_more_info": action.reason, "updated_at": datetime.now(timezone.utc)}})
    db["justification"].update_one({"_id": ObjectId(jid)}, {"$set": {"status": "NeedsMoreInfo", "updated_at": datetime.now(timezone.utc)}})

    just = db["justification"].find_one({"_id": ObjectId(jid)})
    send_email_stub([just.get("requester_email")], "More information requested", f"Approver requested more info: {action.reason}")

    log_audit("justification", jid, "REQUEST_INFO", action.actor_email, {"task_id": task_id, "reason": action.reason})
    return {"ok": True}


@app.post("/api/justifications/{jid}/resubmit")
def resubmit(jid: str, payload: ResubmitPayload):
    just = db["justification"].find_one({"_id": ObjectId(jid)})
    if not just:
        raise HTTPException(404, "Not found")
    db["justification"].update_one({"_id": just["_id"]}, {"$set": {"status": "PendingApproval", "updated_at": datetime.now(timezone.utc)}})
    db["approvaltask"].update_many({"justification_id": jid, "status": "NeedsMoreInfo"}, {"$set": {"status": "Pending", "updated_at": datetime.now(timezone.utc)}})

    log_audit("justification", jid, "RESUBMIT", payload.actor_email, {"message": payload.message})
    send_email_stub([payload.actor_email], "Resubmitted", "Your justification was resubmitted.")
    return {"ok": True}


@app.post("/api/justifications/{jid}/comments")
def add_comment(jid: str, payload: CommentCreate):
    if not db["justification"].find_one({"_id": ObjectId(jid)}):
        raise HTTPException(404, "Justification not found")
    cid = db["comment"].insert_one({
        "justification_id": jid,
        "author_email": payload.author_email,
        "message": payload.message,
        "is_internal": payload.is_internal,
        "created_at": datetime.now(timezone.utc),
    }).inserted_id
    log_audit("justification", jid, "COMMENT", payload.author_email, {"comment_id": str(cid)})
    return {"id": str(cid)}


@app.get("/api/rules")
def list_rules():
    return [serialize_id(r) for r in db["routingrule"].find({}).sort("name", 1)]


@app.post("/api/rules")
def create_rule(rule: Dict[str, Any]):
    if not rule.get("name"):
        raise HTTPException(400, "name required")
    rid = db["routingrule"].insert_one({
        **rule,
        "created_at": datetime.now(timezone.utc),
    }).inserted_id
    return {"id": str(rid)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
