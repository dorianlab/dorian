from typing import Optional, Dict, Any, List
from pydantic import BaseModel
from fastapi import HTTPException, FastAPI
from fastapi.responses import JSONResponse
from urllib.parse import urlencode
import motor.motor_asyncio
import hashlib
import hmac


UUID = str
Column = str

app = FastAPI()
client = motor.motor_asyncio.AsyncIOMotorClient(
    "mongodb://dorian:9dc6babe9fd34cf2adff219cec04d1cc@127.0.0.1:27017/dorian", uuidRepresentation="standard"
)
db = client['dorian']
secret = "56fc958a-b9ee-44f0-8f24-e4fc3a8fdbbc"


class User(BaseModel):
    id: UUID
    name: str
    data: Optional[List[UUID]] = None
    secret: str


class Data(BaseModel):
    id: UUID
    fpath: str
    user_id: UUID
    name: Optional[str] = None
    target: Optional[Column] = None
    categorical: Optional[List[Column]] = None
    meta: Optional[Dict[str, Any]] = None
    secret: str


class Query(BaseModel):
    expr: Dict[str, Any]
    secret: str


def validate(data):
    payload = data.model_dump()
    sign = payload['secret']
    del payload['secret']
    print(payload)
    query = urlencode(payload, True).replace("%40", "@").encode("utf-8")
    signature = hmac.new(secret.encode('utf8'), query, hashlib.sha256).hexdigest()
    return signature == sign


async def common(payload, collection):
    if not validate(payload):
        raise HTTPException(status_code=403, detail="wrong payload")
    data = payload.model_dump()
    del data['secret']
    res = await db[collection].find_one_and_replace({'id': {'$eq': payload.id}}, data, upsert=True)
    if res and '_id' in res:
        del res['_id']
    return res


@app.post("/users")
async def write(payload: User):
    return JSONResponse(content=await common(payload, 'users'))


@app.post("/data")
async def write(payload: Data):
    return JSONResponse(content=await common(payload, 'data'))


@app.get("/{collection}")
async def read(collection: str, payload: Query):
    if not validate(payload):
        raise HTTPException(status_code=403, detail="wrong payload")
    return await db[collection].find(payload.expr, {'_id': False}).to_list(length=None)


