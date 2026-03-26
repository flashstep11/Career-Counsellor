"""
Meeting API routes for booking, listing, joining, and cancelling meetings.
"""
from fastapi import APIRouter, HTTPException, Depends, Query, status
from typing import Optional
from datetime import datetime
from pydantic import BaseModel
from bson import ObjectId
from app.managers.meeting import MeetingManager
from app.managers.expert import ExpertManager
from app.core.auth_utils import require_user, get_current_user
from app.core.time_utils import now_app_naive, parse_app_naive, app_naive_to_epoch_seconds

router = APIRouter()
meeting_manager = MeetingManager()
expert_manager = ExpertManager()


class BookMeetingRequest(BaseModel):
    expertId: str
    startTime: str
    endTime: str


@router.get("/experts/{expert_id}/slots")
async def get_available_slots(
    expert_id: str,
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
):
    """
    Get available 1-hour meeting slots for an expert on a specific date.
    """
    slots = await meeting_manager.get_available_slots(expert_id, date)
    return {"slots": slots}


@router.get("/experts/{expert_id}/availability")
async def get_month_availability(
    expert_id: str,
    year: int = Query(..., description="Year (e.g., 2024)"),
    month: int = Query(..., description="Month (1-12)"),
):
    """
    Get availability for an entire month.
    Returns a dictionary mapping 'YYYY-MM-DD' to boolean indicating if the 
    expert has at least one available slot that day.
    """
    availability = await meeting_manager.get_month_availability(expert_id, year, month)
    return {"availability": availability}


@router.post("/meetings/book")
async def book_meeting(
    request: BookMeetingRequest,
    user_data: dict = Depends(require_user),
):
    """
    Book a meeting with an expert. Deducts coins from the student's wallet
    and creates a Jitsi room for the video call.
    """
    try:
        # Get the current user
        from app.managers.user import UserManager
        user_manager = UserManager()
        user = await user_manager.get_user_by_email(user_data["email"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        start_time = parse_app_naive(request.startTime)
        end_time = parse_app_naive(request.endTime)

        # Server-side validation: reject bookings in the past
        if start_time < now_app_naive():
            raise HTTPException(status_code=400, detail="Cannot book a slot in the past")

        meeting = await meeting_manager.book_meeting(
            expert_id=request.expertId,
            user_id=user.id,
            start_time=start_time,
            end_time=end_time,
        )

        if not meeting:
            raise HTTPException(status_code=500, detail="Failed to book meeting")

        return {
            "success": True,
            "message": "Meeting booked successfully",
            "meeting": meeting,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error booking meeting: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to book meeting: {str(e)}")


@router.get("/meetings/my")
async def get_my_meetings(
    status_filter: Optional[str] = Query(None, alias="status"),
    user_data: dict = Depends(require_user),
):
    """
    Get all meetings for the currently logged-in user (student).
    """
    from app.managers.user import UserManager
    user_manager = UserManager()
    user = await user_manager.get_user_by_email(user_data["email"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    meetings = await meeting_manager.get_all_meetings_for_user(user.id, status_filter)
    return {"meetings": meetings}


@router.get("/meetings/my-earnings")
async def get_my_earnings(
    user_data: dict = Depends(require_user),
):
    """
    Get full earnings breakdown for the authenticated expert.
    Returns total, per-month chart, and per-session detail.
    """
    from app.managers.user import UserManager
    user_manager = UserManager()
    user = await user_manager.get_user_by_email(user_data["email"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    expert = await expert_manager.get_expert_by_user_id(user.id)
    if not expert:
        raise HTTPException(status_code=403, detail="Expert profile not found")

    earnings = await meeting_manager.get_expert_earnings(expert.expertID)
    return earnings


@router.get("/meetings/expert/{expert_id}")
async def get_expert_meetings(
    expert_id: str,
    status_filter: Optional[str] = Query(None, alias="status"),
    user_data: dict = Depends(require_user),
):
    """
    Get all meetings for an expert. Only the expert themselves can view this.
    """
    from app.managers.user import UserManager
    user_manager = UserManager()
    user = await user_manager.get_user_by_email(user_data["email"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    expert = await expert_manager.get_expert(expert_id)
    if not expert:
        raise HTTPException(status_code=404, detail="Expert not found")

    if expert.userId != user.id:
        raise HTTPException(status_code=403, detail="You can only view your own meetings")

    meetings = await meeting_manager.get_expert_meetings(expert_id, status_filter)
    return {"meetings": meetings}


@router.get("/meetings/{meeting_id}")
async def get_meeting(
    meeting_id: str,
    user_data: dict = Depends(require_user),
):
    """
    Get a single meeting by ID. Only participants can view it.
    """
    meeting = await meeting_manager.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    from app.managers.user import UserManager
    user_manager = UserManager()
    user = await user_manager.get_user_by_email(user_data["email"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    is_student = meeting["userId"] == user.id
    is_expert = False
    expert = await expert_manager.get_expert(meeting["expertId"])
    if expert and expert.userId == user.id:
        is_expert = True

    if not is_student and not is_expert:
        raise HTTPException(status_code=403, detail="You are not a participant of this meeting")

    return {"meeting": meeting}


@router.get("/meetings/{meeting_id}/token")
async def get_meeting_token(
    meeting_id: str,
    user_data: dict = Depends(require_user),
):
    """
    Get room credentials for joining a meeting.
    Uses free public Jitsi (meet.jit.si) — no JWT required.
    Expert gets startAsModerator flag. Room name is a secret UUID so outsiders cannot guess it.
    Time-gated to 10 minutes before start.
    """
    meeting = await meeting_manager.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if meeting["status"] == "cancelled":
        raise HTTPException(status_code=400, detail="This meeting has been cancelled")

    # Time-gate: only allow joining within 10 minutes before the meeting start
    meeting_start = meeting.get("startTime")
    if meeting_start:
        meeting_start = parse_app_naive(meeting_start)
        minutes_until_start = (meeting_start - now_app_naive()).total_seconds() / 60
        if minutes_until_start > 10:
            raise HTTPException(
                status_code=400,
                detail="Meeting has not started yet. You can join 10 minutes before the scheduled time."
            )

    # Determine if the caller is the student or the expert
    from app.managers.user import UserManager
    user_manager = UserManager()
    user = await user_manager.get_user_by_email(user_data["email"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    is_student = meeting["userId"] == user.id
    is_expert = False
    expert = await expert_manager.get_expert(meeting["expertId"])
    if expert and expert.userId == user.id:
        is_expert = True

    if not is_student and not is_expert:
        raise HTTPException(status_code=403, detail="You are not a participant of this meeting")

    user_name = f"{user.firstName} {user.lastName}"

    # Get or generate a secret UUID-based room slug (stored in DB)
    room_slug = meeting.get("jitsiRoomName") or meeting.get("dailyRoomName", "")
    if not room_slug:
        import uuid
        room_slug = f"alumniti-{uuid.uuid4().hex[:12]}"
        from app.core.database import get_database
        from bson import ObjectId as _OID
        db = get_database()
        await db.meetings.update_one(
            {"_id": _OID(meeting_id)},
            {"$set": {"jitsiRoomName": room_slug}}
        )

    meeting_end = meeting.get("endTime")
    meeting_end = parse_app_naive(meeting_end)

    # --- JaaS RS256 JWT ---
    import jwt as pyjwt
    import time
    from pathlib import Path
    from app.config import settings

    key_path = Path(__file__).parent.parent / "jaas_private.key"
    private_key = key_path.read_text()

    exp_ts = app_naive_to_epoch_seconds(meeting_end) if meeting_end else int(time.time()) + 7200
    payload = {
        "iss": "chat",
        "aud": "jitsi",
        "iat": int(time.time()),
        "nbf": int(time.time()) - 10,
        "exp": exp_ts,
        "sub": settings.JAAS_APP_ID,
        "context": {
            "user": {
                "name": user_name,
                "affiliation": "owner" if is_expert else "member",
                "moderator": "true" if is_expert else "false",
            },
            "features": {
                "livestreaming": "false",
                "recording": "false",
                "transcription": "false",
                "outbound-call": "false",
            },
        },
        "room": "*",
    }
    jaas_jwt = pyjwt.encode(
        payload, private_key, algorithm="RS256",
        headers={"kid": settings.JAAS_KEY_ID}
    )

    # Full room name for JitsiMeetExternalAPI: APP_ID/slug
    room_name = f"{settings.JAAS_APP_ID}/{room_slug}"

    # Compute extension cost for 30 min
    extension_cost_30min = 0
    if expert and not is_expert:
        session_mins = int(getattr(expert, "sessionDurationMinutes", None) or 60)
        hourly_rate = float(getattr(expert, "meetingCost", None) or 0)
        extension_cost_30min = int(hourly_rate * (30 / max(session_mins, 1)))

    # Fetch student wallet balance
    wallet_balance = 0
    if not is_expert:
        user_doc = await meeting_manager.db.users.find_one({"_id": ObjectId(user.id)})
        if user_doc:
            wallet_balance = user_doc.get("wallet", 0)

    return {
        "roomName": room_name,
        "jwt": jaas_jwt,
        "userName": user_name,
        "isOwner": is_expert,
        "endTime": meeting_end.isoformat() if meeting_end else None,
        "extensionCost30min": extension_cost_30min,
        "walletBalance": wallet_balance,
    }


@router.post("/meetings/{meeting_id}/cancel")
async def cancel_meeting(
    meeting_id: str,
    user_data: dict = Depends(require_user),
):
    """
    Cancel a meeting. Refunds coins to the student's wallet.
    """
    from app.managers.user import UserManager
    user_manager = UserManager()
    user = await user_manager.get_user_by_email(user_data["email"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    success = await meeting_manager.cancel_meeting(meeting_id, user.id)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to cancel meeting")
    return {"success": True, "message": "Meeting cancelled successfully"}


class ExtendMeetingRequest(BaseModel):
    durationMinutes: int = 30


@router.post("/meetings/{meeting_id}/extend")
async def extend_meeting(
    meeting_id: str,
    request: ExtendMeetingRequest,
    user_data: dict = Depends(require_user),
):
    """
    Extend a meeting by a certain number of minutes. Deducts coins from student.
    Returns the new meeting end time and updated wallet balance so the frontend
    can call /token again for a fresh JWT with the extended expiry.
    """
    from app.managers.user import UserManager
    user_manager = UserManager()
    user = await user_manager.get_user_by_email(user_data["email"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    success, message = await meeting_manager.extend_meeting(
        meeting_id, user.id, request.durationMinutes
    )
    if not success:
        raise HTTPException(status_code=400, detail=message)

    # Return new meeting end time and wallet balance so frontend can refresh token
    db = meeting_manager.db
    updated_meeting = await db.meetings.find_one({"_id": ObjectId(meeting_id)})
    updated_user = await db.users.find_one({"_id": ObjectId(user.id)})
    new_end_time = updated_meeting.get("endTime") if updated_meeting else None
    new_wallet = updated_user.get("wallet", 0) if updated_user else 0

    return {
        "success": True,
        "message": message,
        "newEndTime": new_end_time.isoformat() if new_end_time else None,
        "newWalletBalance": new_wallet,
    }
