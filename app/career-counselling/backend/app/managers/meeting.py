"""
MeetingManager handles booking logic, slot calculation, and meeting lifecycle.
"""
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from bson import ObjectId
from app.core.database import get_database
from app.models.meeting import MeetingStatus
import uuid
from app.core.time_utils import now_app_naive


class MeetingManager:
    def __init__(self):
        self.db = get_database()
        self.collection = self.db.meetings

    async def get_available_slots(self, expert_id: str, date_str: str) -> List[Dict]:
        """
        Calculate available 1-hour slots for an expert on a given date.

        Args:
            expert_id: The expert's ID
            date_str: Date in "YYYY-MM-DD" format

        Returns:
            List of available time slots
        """
        # Get the expert's availability settings
        expert = await self.db.experts.find_one({"_id": ObjectId(expert_id)})
        if not expert:
            return []

        availability = expert.get("availability")
        if not availability:
            return []

        # Determine the day of the week for the requested date
        requested_date = datetime.strptime(date_str, "%Y-%m-%d")
        day_name = requested_date.strftime("%A").lower()  # e.g., "monday"
        print("REQUESTED DAY:", day_name)

        day_config = availability.get(day_name)
        print("DAY CONFIG:", day_config)
        if not day_config or not day_config.get("isAvailable", False):
            print("DAY NOT AVAILABLE")
            return []

        # Get existing bookings for this expert on the requested date
        start_of_day = requested_date.replace(hour=0, minute=0, second=0)
        end_of_day = requested_date.replace(hour=23, minute=59, second=59)

        existing_bookings = await self.collection.find({
            "expertId": expert_id,
            "startTime": {"$gte": start_of_day, "$lte": end_of_day},
            "status": {"$ne": MeetingStatus.CANCELLED},
        }).to_list(None)

        booked_times = set()
        for booking in existing_bookings:
            booked_times.add(booking["startTime"].strftime("%H:%M"))

        # Per-expert session duration (default 60 min)
        slot_duration = int(expert.get("sessionDurationMinutes", 60))
        slot_td = timedelta(minutes=slot_duration)

        # Build available slots from the expert's configured time windows
        available_slots = []
        for slot in day_config.get("slots", []):
            start_time_str = slot.get("startTime", "")  # "09:00"
            end_time_str = slot.get("endTime", "")       # "17:00"
            print(f"CHECKING SLOT {start_time_str} TO {end_time_str}")

            if not start_time_str or not end_time_str:
                continue

            start_hour, start_min = map(int, start_time_str.split(":"))
            end_hour, end_min = map(int, end_time_str.split(":"))

            current = requested_date.replace(hour=start_hour, minute=start_min, second=0)
            end = requested_date.replace(hour=end_hour, minute=end_min, second=0)
            # Handle midnight wraparound: '23:00' -> '00:00' means end of day
            if end <= current:
                end += timedelta(days=1)

            while current + slot_td <= end:
                slot_time_str = current.strftime("%H:%M")
                slot_end = current + slot_td
                print(f"  INNER WHILE: slot {slot_time_str}")

                # Skip if already booked
                if slot_time_str not in booked_times:
                    # Skip if the slot is in the past.
                    # Use datetime.now() (server local time) instead of
                    # datetime.utcnow() so the comparison matches the naive
                    # local-date that the frontend sends.
                    if current > now_app_naive():
                        print(f"    ADDED SLOT {slot_time_str}")
                        available_slots.append({
                            "startTime": current.isoformat(),
                            "endTime": slot_end.isoformat(),
                            "display": f"{current.strftime('%I:%M %p')} - {slot_end.strftime('%I:%M %p')}",
                        })
                    else:
                        print(f"    SKIPPED: {current} is <= {now_app_naive()} (IN THE PAST)")
                else:
                    print(f"    SKIPPED: {slot_time_str} is already booked")

                current += slot_td

        print("FINAL SLOTS:", available_slots)
        return available_slots

    async def get_month_availability(self, expert_id: str, year: int, month: int) -> Dict[str, bool]:
        """
        Calculate availability for all days in a given month.
        Returns a dictionary mapping 'YYYY-MM-DD' dates to a boolean indicating
        if the expert has at least one available slot on that date.
        """
        # Quick check for expert availability config
        expert = await self.db.experts.find_one({"_id": ObjectId(expert_id)})
        if not expert or not expert.get("availability"):
            return {}
            
        # Find the number of days in the requested month
        import calendar
        _, num_days = calendar.monthrange(year, month)
        
        # Get all bookings for the entire month for this expert
        start_of_month = datetime(year, month, 1)
        end_of_month = datetime(year, month, num_days, 23, 59, 59)
        
        existing_bookings = await self.collection.find({
            "expertId": expert_id,
            "startTime": {"$gte": start_of_month, "$lte": end_of_month},
            "status": {"$ne": MeetingStatus.CANCELLED},
        }).to_list(None)
        
        # Group bookings by date string
        bookings_by_date = {}
        for booking in existing_bookings:
            date_str = booking["startTime"].strftime("%Y-%m-%d")
            time_str = booking["startTime"].strftime("%H:%M")
            if date_str not in bookings_by_date:
                bookings_by_date[date_str] = set()
            bookings_by_date[date_str].add(time_str)
            
        availability_config = expert.get("availability")
        slot_duration = int(expert.get("sessionDurationMinutes", 60))
        slot_td = timedelta(minutes=slot_duration)
        result = {}
        
        now = now_app_naive()
        
        # Calculate availability for each day
        for day in range(1, num_days + 1):
            test_date = datetime(year, month, day)
            date_str = test_date.strftime("%Y-%m-%d")
            day_name = test_date.strftime("%A").lower()
            
            # If the date is entirely in the past (before today), it's not available
            if test_date.date() < now.date():
                result[date_str] = False
                continue
                
            day_config = availability_config.get(day_name)
            
            # If day is not configured as available
            if not day_config or not day_config.get("isAvailable", False):
                result[date_str] = False
                continue
                
            booked_times = bookings_by_date.get(date_str, set())
            has_available_slot = False
            
            # Check if there is at least one unbooked slot in the future
            for slot in day_config.get("slots", []):
                start_time_str = slot.get("startTime", "")
                end_time_str = slot.get("endTime", "")
                
                if not start_time_str or not end_time_str:
                    continue
                    
                start_hour, start_min = map(int, start_time_str.split(":"))
                end_hour, end_min = map(int, end_time_str.split(":"))
                
                current = test_date.replace(hour=start_hour, minute=start_min, second=0)
                end = test_date.replace(hour=end_hour, minute=end_min, second=0)
                # Handle midnight wraparound
                if end <= current:
                    end += timedelta(days=1)
                
                while current + slot_td <= end:
                    slot_time_str = current.strftime("%H:%M")
                    
                    # Check if this specific slot is available
                    if slot_time_str not in booked_times and current > now:
                        has_available_slot = True
                        break # Found at least one slot, day is available
                        
                    current += slot_td
                    
                if has_available_slot:
                    break
                    
            result[date_str] = has_available_slot
            
        return result

    async def book_meeting(
        self,
        expert_id: str,
        user_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> Optional[Dict]:
        """
        Book a meeting between a student and an expert.

        Steps:
        1. Verify the slot is still available
        2. Get expert's meeting cost
        3. Deduct coins from user's wallet
        4. Generate a Jitsi room name
        5. Save meeting to DB

        Returns:
            Meeting dict or None on failure
        """
        # 1. Get expert details
        expert = await self.db.experts.find_one({"_id": ObjectId(expert_id)})
        if not expert:
            return None

        # 2. Check if the slot is still available (no conflicting booking)
        conflict = await self.collection.find_one({
            "expertId": expert_id,
            "startTime": start_time,
            "status": {"$ne": MeetingStatus.CANCELLED},
        })
        if conflict:
            raise ValueError("This time slot is no longer available")

        # 3. Get cost and check user's wallet balance
        cost = expert.get("meetingCost", 0)
        user = await self.db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise ValueError("User not found")

        wallet_balance = user.get("wallet", 0)
        if wallet_balance < cost:
            raise ValueError(f"Insufficient balance. You need {cost} coins but have {wallet_balance}")

        # 4. Deduct coins from user's wallet
        await self.db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$inc": {"wallet": -int(cost)}}
        )

        # 5. Generate a Jitsi room name (no API call needed)
        room_name = f"alumniti-meeting-{uuid.uuid4().hex[:12]}"
        jitsi_room_url = f"https://meet.jit.si/{room_name}"

        # 6. Save meeting to DB
        now = datetime.utcnow()
        meeting_doc = {
            "expertId": expert_id,
            "userId": user_id,
            "startTime": start_time,
            "endTime": end_time,
            "costInfo": cost,
            "status": MeetingStatus.SCHEDULED,
            "jitsiRoomUrl": jitsi_room_url,
            "jitsiRoomName": room_name,
            "createdAt": now,
            "updatedAt": now,
        }

        result = await self.collection.insert_one(meeting_doc)
        meeting_doc["meetingId"] = str(result.inserted_id)
        meeting_doc["_id"] = str(result.inserted_id)

        return meeting_doc

    async def get_meeting(self, meeting_id: str) -> Optional[Dict]:
        """Get a single meeting by ID."""
        try:
            meeting = await self.collection.find_one({"_id": ObjectId(meeting_id)})
            if meeting:
                meeting["meetingId"] = str(meeting["_id"])
                meeting["_id"] = str(meeting["_id"])
            return meeting
        except Exception as e:
            print(f"Error getting meeting: {e}")
            return None

    async def get_user_meetings(self, user_id: str, status: Optional[str] = None) -> List[Dict]:
        """Get all meetings for a user (student)."""
        query = {"userId": user_id}
        if status:
            query["status"] = status

        meetings = await self.collection.find(query).sort("startTime", 1).to_list(None)
        for m in meetings:
            m["meetingId"] = str(m["_id"])
            m["_id"] = str(m["_id"])
            # Map costInfo to amount for UI
            m["amount"] = m.get("costInfo") or 0
            m["isPaid"] = m["amount"] > 0
            # Map jitsiRoomUrl to meetingLink for UI — always set it
            m["meetingLink"] = m.get("jitsiRoomUrl") or m.get("dailyRoomUrl") or f"https://meet.jit.si/{m.get('jitsiRoomName', m.get('dailyRoomName', 'unknown'))}"
                
            # Attach expert name
            expert = await self.db.experts.find_one({"_id": ObjectId(m["expertId"])})
            if expert:
                user_doc = await self.db.users.find_one({"_id": ObjectId(expert["userId"])})
                if user_doc:
                    m["expertName"] = f"{user_doc.get('firstName', '')} {user_doc.get('lastName', '')}"
                    m["expertPosition"] = expert.get("currentPosition", "")
        return meetings

    async def get_expert_meetings(self, expert_id: str, status: Optional[str] = None) -> List[Dict]:
        """Get all meetings for an expert."""
        query = {"expertId": expert_id}
        if status:
            query["status"] = status

        meetings = await self.collection.find(query).sort("startTime", 1).to_list(None)
        for m in meetings:
            m["meetingId"] = str(m["_id"])
            m["_id"] = str(m["_id"])
            # Attach student name
            user_doc = await self.db.users.find_one({"_id": ObjectId(m["userId"])})
            if user_doc:
                m["studentName"] = f"{user_doc.get('firstName', '')} {user_doc.get('lastName', '')}"
            # Also set meetingLink for expert-side UI
            m["meetingLink"] = m.get("jitsiRoomUrl") or m.get("dailyRoomUrl") or f"https://meet.jit.si/{m.get('jitsiRoomName', m.get('dailyRoomName', 'unknown'))}"
        return meetings

    async def get_all_meetings_for_user(self, user_id: str, status: Optional[str] = None) -> List[Dict]:
        """Get all meetings where the user is either the student OR the expert."""
        # Find expert profile for this user (if any)
        expert = await self.db.experts.find_one({"userId": user_id})
        expert_id = str(expert["_id"]) if expert else None

        # Build query: meetings where user is student OR expert
        conditions = [{"userId": user_id}]
        if expert_id:
            conditions.append({"expertId": expert_id})

        query: dict = {"$or": conditions}
        if status:
            query["status"] = status

        meetings = await self.collection.find(query).sort("startTime", 1).to_list(None)
        seen_ids = set()
        unique_meetings = []
        for m in meetings:
            mid = str(m["_id"])
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            m["meetingId"] = mid
            m["_id"] = mid
            m["amount"] = m.get("costInfo") or 0
            m["isPaid"] = m["amount"] > 0
            m["meetingLink"] = m.get("jitsiRoomUrl") or m.get("dailyRoomUrl") or f"https://meet.jit.si/{m.get('jitsiRoomName', m.get('dailyRoomName', 'unknown'))}"

            # Attach expert name
            exp = await self.db.experts.find_one({"_id": ObjectId(m["expertId"])})
            if exp:
                user_doc = await self.db.users.find_one({"_id": ObjectId(exp["userId"])})
                if user_doc:
                    m["expertName"] = f"{user_doc.get('firstName', '')} {user_doc.get('lastName', '')}"
                    m["expertPosition"] = exp.get("currentPosition", "")

            # Attach student name
            student_doc = await self.db.users.find_one({"_id": ObjectId(m["userId"])})
            if student_doc:
                m["studentName"] = f"{student_doc.get('firstName', '')} {student_doc.get('lastName', '')}"

            unique_meetings.append(m)
        return unique_meetings

    async def cancel_meeting(self, meeting_id: str, user_id: str) -> bool:
        """
        Cancel a meeting. Refund coins to the student's wallet.
        Only the student who booked or the expert can cancel.
        """
        meeting = await self.get_meeting(meeting_id)
        if not meeting:
            return False

        # Verify the user is either the student or the expert
        if meeting["userId"] != user_id:
            # Check if user is the expert
            expert = await self.db.experts.find_one({"_id": ObjectId(meeting["expertId"])})
            if not expert or expert["userId"] != user_id:
                return False

        if meeting["status"] == MeetingStatus.CANCELLED:
            return False  # Already cancelled

        # Refund coins to the student
        cost = meeting.get("costInfo", 0)
        if cost > 0:
            await self.db.users.update_one(
                {"_id": ObjectId(meeting["userId"])},
                {"$inc": {"wallet": int(cost)}}
            )

        # Update meeting status
        await self.collection.update_one(
            {"_id": ObjectId(meeting_id)},
            {"$set": {"status": MeetingStatus.CANCELLED, "updatedAt": datetime.utcnow()}}
        )

        # No need to delete Jitsi rooms — they auto-expire
        return True

    async def extend_meeting(
        self,
        meeting_id: str,
        user_id: str,
        duration_minutes: int = 30
    ) -> tuple[bool, str]:
        """
        Extend an ongoing meeting by the specified duration.
        Checks for conflicts for both the expert AND the student.
        """
        meeting = await self.get_meeting(meeting_id)
        if not meeting:
            return False, "Meeting not found"

        # Only the student can extend (and pay)
        if str(meeting["userId"]) != str(user_id):
            return False, "Only the student who booked the meeting can extend it"

        # Accept both string and enum values for status
        allowed_statuses = {MeetingStatus.SCHEDULED, MeetingStatus.IN_PROGRESS, "scheduled", "in_progress"}
        if meeting["status"] not in allowed_statuses:
            return False, "Only active or scheduled meetings can be extended"

        expert_id = meeting["expertId"]
        current_end = meeting["endTime"]
        
        # Ensure current_end is a datetime object
        if isinstance(current_end, str):
            current_end = datetime.fromisoformat(current_end.replace('Z', '+00:00'))

        new_end = current_end + timedelta(minutes=duration_minutes)

        # 1. Check for conflicts for this EXPERT (any meeting starting in the extended window)
        expert_conflict = await self.collection.find_one({
            "_id": {"$ne": ObjectId(meeting_id)},
            "expertId": expert_id,
            "status": {"$ne": MeetingStatus.CANCELLED},
            "startTime": {"$lt": new_end, "$gte": current_end}
        })
        if expert_conflict:
            return False, "Cannot extend — the expert has another meeting scheduled right after this one."

        # 2. Check for conflicts for the STUDENT (any meeting starting in the extended window)
        student_conflict = await self.collection.find_one({
            "_id": {"$ne": ObjectId(meeting_id)},
            "userId": str(user_id),
            "status": {"$ne": MeetingStatus.CANCELLED},
            "startTime": {"$lt": new_end, "$gte": current_end}
        })
        if student_conflict:
            return False, "Cannot extend — you have another meeting scheduled right after this one."

        # 3. Calculate prorated cost
        expert = await self.db.experts.find_one({"_id": ObjectId(expert_id)})
        if not expert:
            return False, "Expert not found"

        hourly_rate = expert.get("meetingCost", 0)
        extension_cost = int(hourly_rate * (duration_minutes / 60.0))

        # 4. Check wallet balance
        user = await self.db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            return False, "User not found"

        wallet_balance = user.get("wallet", 0)
        if wallet_balance < extension_cost:
            return False, f"Insufficient balance. You need {extension_cost} coins but have {wallet_balance}"

        # 5. Deduct coins from user's wallet
        await self.db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$inc": {"wallet": -extension_cost}}
        )

        # 6. Update meeting end time; track extension earnings separately (costInfo stays as original booking cost)
        await self.collection.update_one(
            {"_id": ObjectId(meeting_id)},
            {
                "$set": {
                    "endTime": new_end,
                    "updatedAt": datetime.utcnow()
                },
                "$inc": {
                    "extensionEarnings": extension_cost
                }
            }
        )

        return True, "Meeting extended successfully"

    async def complete_meeting(self, meeting_id: str) -> bool:
        """Mark a meeting as completed."""
        result = await self.collection.update_one(
            {"_id": ObjectId(meeting_id)},
            {"$set": {"status": MeetingStatus.COMPLETED, "updatedAt": datetime.utcnow()}}
        )
        return result.modified_count > 0

    async def get_expert_earnings(self, expert_id: str) -> dict:
        """
        Calculate full earnings breakdown for an expert.
        - base_earnings = sum of costInfo for non-cancelled meetings
        - extension_earnings = sum of extensionEarnings across all meetings
        - Per-month breakdown for the last 12 months.
        - Per-session list.
        """
        from collections import defaultdict

        meetings = await self.collection.find({"expertId": expert_id}).sort("startTime", -1).to_list(None)

        total_base = 0
        total_extensions = 0
        monthly: dict = defaultdict(lambda: {"base": 0, "extensions": 0, "sessions": 0})
        sessions = []

        for m in meetings:
            base = m.get("costInfo", 0) or 0
            ext = m.get("extensionEarnings", 0) or 0
            status = m.get("status", "")
            start = m.get("startTime")
            if isinstance(start, str):
                start = datetime.fromisoformat(start)

            month_key = start.strftime("%b %Y") if start else "Unknown"

            # Base earning: counted only if not cancelled (non-refunded)
            if status != MeetingStatus.CANCELLED:
                total_base += base
                monthly[month_key]["base"] += base
                monthly[month_key]["sessions"] += 1

            # Extension earnings: always kept (non-refundable)
            total_extensions += ext
            monthly[month_key]["extensions"] += ext

            # Attach student name
            student_name = "Unknown"
            try:
                student = await self.db.users.find_one({"_id": ObjectId(m["userId"])})
                if student:
                    student_name = f"{student.get('firstName','')} {student.get('lastName','')}".strip()
            except Exception:
                pass

            sessions.append({
                "meetingId": str(m["_id"]),
                "studentName": student_name,
                "startTime": start.isoformat() if start else None,
                "status": status,
                "baseCost": base,
                "extensionEarnings": ext,
                "totalEarned": (base if status != MeetingStatus.CANCELLED else 0) + ext,
            })

        now = datetime.now()
        this_month_key = now.strftime("%b %Y")
        last_month_key = (now.replace(day=1) - timedelta(days=1)).strftime("%b %Y")

        # Build last-12-month chart data in order
        chart = []
        for i in range(11, -1, -1):
            from datetime import date
            import calendar as cal_mod
            d = date(now.year, now.month, 1)
            # go back i months
            month_num = now.month - i
            year_num = now.year
            while month_num <= 0:
                month_num += 12
                year_num -= 1
            key = datetime(year_num, month_num, 1).strftime("%b %Y")
            short = datetime(year_num, month_num, 1).strftime("%b")
            data = monthly.get(key, {"base": 0, "extensions": 0, "sessions": 0})
            chart.append({
                "month": short,
                "fullMonth": key,
                "base": data["base"],
                "extensions": data["extensions"],
                "total": data["base"] + data["extensions"],
                "sessions": data["sessions"],
            })

        return {
            "totalEarnings": total_base + total_extensions,
            "baseEarnings": total_base,
            "extensionEarnings": total_extensions,
            "thisMonth": monthly[this_month_key]["base"] + monthly[this_month_key]["extensions"],
            "lastMonth": monthly[last_month_key]["base"] + monthly[last_month_key]["extensions"],
            "chart": chart,
            "sessions": sessions,
        }
