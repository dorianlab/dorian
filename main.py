from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    UploadFile,
    HTTPException,
    Depends,
    WebSocket,
    WebSocketDisconnect
)
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from collections import defaultdict
from dynaconf import Dynaconf
from pathlib import Path
from redis import asyncio as aioredis
import aiofiles
import asyncio
import json
import sys

from backend.events import emit, Event, subscribe
from backend.models import User


config = Dynaconf(settings_files=['config.yaml'])
config = config[config.type]


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = aioredis.from_url(config.redis.host)
    yield
    

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# users = defaultdict(lambda: User(login="tmp"))


@app.get("/favicon.ico")
async def favicon():
    return FileResponse('frontend/public/favicon.svg')


# subscribe("DataWritten", lambda event: event)

@app.post("/upload")
async def upload_data(file: UploadFile = File(...)):
    # if token not in users:
    # 	return HTTPException(status_code=401, detail="Access Denied")
    fpath = Path(config['folder']['data']) / f'tmp/{file.filename}' # {users[token].login}
    meta = {
        'uid': None, # users[token].login,
        'fpath': fpath.absolute().as_posix(),
    }

    if fpath.exists():
        emit(Event(type="DataExists", data=meta))
        return {"status": "OK"}

    fpath.parent.mkdir(exist_ok=True, parents=True)
    emit(Event(type="WritingData", data=meta))
    async with aiofiles.open(fpath, 'wb') as out:
        while content := await file.read(1024):  # async read chunk
            await out.write(content)  # async write chunk
    emit(Event(type='DataWritten', data=meta))
    return {"status": "OK"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(data)
    except WebSocketDisconnect:
        pass

    async def receive_messages():
        while True:
            try:
                message = await websocket.receive_text()
                emit(Event(type='EventReceived', data=json.loads(message)))
            except WebSocketDisconnect:
                pass
            except Exception as e:
                _type, _value, _ = sys.exc_info()
                emit(Event(type='WebsocketErrorOnReceive', data={'error': _type.__name__, 'message': _value}))

    async def send_messages():
        while True:
            try:
                message = await message_queue.get()
                await websocket.send_text(message)
                print(f"Sent message: {message}")
            except WebSocketDisconnect:
                pass
            except Exception as e:
                _type, _value, _ = sys.exc_info()
                emit(Event(type='WebsocketErrorOnSend', data={'error': _type.__name__, 'message': _value}))

    receive = asyncio.create_task(receive_messages())
    send = asyncio.create_task(send_messages())

    await asyncio.gather(receive, send)