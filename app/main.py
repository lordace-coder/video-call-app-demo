from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Dict, List, Optional
import json
import logging
import uuid
from datetime import datetime
from pydantic import BaseModel

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Audio Live Calls API", version="1.0.0")


# Models
class CallRoom(BaseModel):
    room_id: str
    created_at: datetime
    participants: List[str] = []


class SignalingMessage(BaseModel):
    type: str
    data: dict
    from_user: str
    to_user: Optional[str] = None


# In-memory storage (use Redis/DB for production)
active_rooms: Dict[str, CallRoom] = {}
websocket_connections: Dict[str, WebSocket] = {}
user_rooms: Dict[str, str] = {}  # user_id -> room_id


# WebRTC signaling server
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    websocket_connections[user_id] = websocket

    logger.info(f"User {user_id} connected")

    try:
        while True:
            # Receive message from client
            data = await websocket.receive_text()
            message_data = json.loads(data)

            await handle_signaling_message(user_id, message_data)

    except WebSocketDisconnect:
        logger.info(f"User {user_id} disconnected")
        await cleanup_user(user_id)
    except Exception as e:
        logger.error(f"Error with user {user_id}: {e}")
        await cleanup_user(user_id)


async def handle_signaling_message(user_id: str, message_data: dict):
    """Handle WebRTC signaling messages"""
    msg_type = message_data.get("type")

    if msg_type == "join-room":
        room_id = message_data.get("room_id")
        await join_room(user_id, room_id)

    elif msg_type in ["offer", "answer", "ice-candidate"]:
        # Forward WebRTC signaling messages to other participants
        room_id = user_rooms.get(user_id)
        if room_id and room_id in active_rooms:
            await broadcast_to_room(
                room_id,
                {
                    "type": msg_type,
                    "data": message_data.get("data"),
                    "from_user": user_id,
                },
                exclude_user=user_id,
            )


async def join_room(user_id: str, room_id: str):
    """Add user to a call room"""
    if room_id not in active_rooms:
        active_rooms[room_id] = CallRoom(room_id=room_id, created_at=datetime.now())

    room = active_rooms[room_id]

    if user_id not in room.participants:
        room.participants.append(user_id)
        user_rooms[user_id] = room_id

        # Notify other participants
        await broadcast_to_room(
            room_id,
            {
                "type": "user-joined",
                "user_id": user_id,
                "participants": room.participants,
            },
            exclude_user=user_id,
        )

        # Send current participants to new user
        if user_id in websocket_connections:
            await websocket_connections[user_id].send_text(
                json.dumps(
                    {
                        "type": "room-joined",
                        "room_id": room_id,
                        "participants": room.participants,
                    }
                )
            )

    logger.info(f"User {user_id} joined room {room_id}")


async def broadcast_to_room(room_id: str, message: dict, exclude_user: str = None):
    """Send message to all participants in a room"""
    if room_id not in active_rooms:
        return

    room = active_rooms[room_id]
    message_json = json.dumps(message)

    for participant_id in room.participants:
        if participant_id != exclude_user and participant_id in websocket_connections:
            try:
                await websocket_connections[participant_id].send_text(message_json)
            except Exception as e:
                logger.error(f"Failed to send message to {participant_id}: {e}")


async def cleanup_user(user_id: str):
    """Clean up user data when disconnecting"""
    # Remove from websocket connections
    if user_id in websocket_connections:
        del websocket_connections[user_id]

    # Remove from room
    room_id = user_rooms.get(user_id)
    if room_id and room_id in active_rooms:
        room = active_rooms[room_id]
        if user_id in room.participants:
            room.participants.remove(user_id)

            # Notify other participants
            await broadcast_to_room(
                room_id,
                {
                    "type": "user-left",
                    "user_id": user_id,
                    "participants": room.participants,
                },
            )

            # Remove empty rooms
            if not room.participants:
                del active_rooms[room_id]

    # Remove from user_rooms mapping
    if user_id in user_rooms:
        del user_rooms[user_id]


# REST API endpoints
@app.post("/rooms")
async def create_room():
    """Create a new call room"""
    room_id = str(uuid.uuid4())
    room = CallRoom(room_id=room_id, created_at=datetime.now())
    active_rooms[room_id] = room

    return {"room_id": room_id, "created_at": room.created_at}


@app.get("/rooms/{room_id}")
async def get_room(room_id: str):
    """Get room information"""
    if room_id not in active_rooms:
        raise HTTPException(status_code=404, detail="Room not found")

    room = active_rooms[room_id]
    return {
        "room_id": room.room_id,
        "created_at": room.created_at,
        "participants": room.participants,
        "participant_count": len(room.participants),
    }


@app.get("/rooms")
async def list_rooms():
    """List all active rooms"""
    return [
        {
            "room_id": room.room_id,
            "created_at": room.created_at,
            "participant_count": len(room.participants),
        }
        for room in active_rooms.values()
    ]


@app.delete("/rooms/{room_id}")
async def delete_room(room_id: str):
    """Delete a room (admin endpoint)"""
    if room_id not in active_rooms:
        raise HTTPException(status_code=404, detail="Room not found")

    # Notify all participants
    await broadcast_to_room(room_id, {"type": "room-closed"})

    # Clean up participants
    room = active_rooms[room_id]
    for participant_id in room.participants.copy():
        await cleanup_user(participant_id)

    del active_rooms[room_id]
    return {"message": "Room deleted"}


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "message": "Audio Live Calls API is running",
        "active_rooms": len(active_rooms),
        "connected_users": len(websocket_connections),
    }


# Mount static files for frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/call/{room_id}")
async def serve_call_page(room_id: str):
    """Serve the call interface"""
    return FileResponse("static/call.html")



