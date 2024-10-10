from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4 as uuid
import openml

from dataclasses import dataclass, asdict
from typing import Optional, Sequence, Dict, Any, List
from pathlib import Path
from urllib.parse import urlencode
from enum import Enum
import requests
import hashlib
import hmac
import sys

UUID = str
Column = str


openml.config.apikey = '024cb6eb1a204a0c93402df65cfd1588'
secret = '56fc958a-b9ee-44f0-8f24-e4fc3a8fdbbc'
uri = 'http://dfki-3112.dfki.de:8000'

base = Path(__file__).parents[1] / 'data/datasets'
base.mkdir(parents=True, exist_ok=True)


class RequestType(Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"


@dataclass
class User:
    id: UUID
    name: str
    data: Optional[List[UUID]] = None


@dataclass
class TabularData:
    id: UUID
    fpath: str
    user_id: UUID
    name: Optional[str] = None
    target: Optional[Column] = None
    categorical: Optional[List[Column]] = None
    meta: Optional[Dict[str, Any]] = None


def request(db: str, payload, mode: RequestType = RequestType.POST):
    query = urlencode(payload, True).replace("%40", "@").encode("utf-8")
    signature = hmac.new(secret.encode('utf8'), query, hashlib.sha256).hexdigest()
    url = f'{uri}/{db}'
    match mode:
        case RequestType.GET:
            response = requests.get(url, verify=False, json=dict(payload, **{'secret': signature}))
        case RequestType.POST:
            response = requests.post(url, verify=False, json=dict(payload, **{'secret': signature}))
        case other:
            response = {"error": "wrong request type"}
    return response


async def main():
    res = request('users', {'expr': {'name': {'$eq': 'dorian'}}}, mode=RequestType.GET).json()
    if not res:
        user = User(uuid().hex, 'dorian')
        request('users', asdict(user))
    else:
        user = User(**res[0])

    datasets = request('data', {'expr': {}}, mode=RequestType.GET).json()
    known = [TabularData(**td).meta['openml_did'] for td in datasets]
    print(known)

    df = openml.datasets.list_datasets(output_format='dataframe')
    dids = df.did.tolist()

    n = len(dids)
    failed = [46, 6332, 40966, 40994]
    for i, did in enumerate(dids):
        if did in failed: continue
        if did in known: continue
        dataset = openml.datasets.get_dataset(did, download_data=True, download_qualities=False,
                                              download_features_meta_data=True)
        X, y, _, _ = dataset.get_data(dataset_format="dataframe")
        if y:
            print(f'y not empty {y}')
        _id = uuid().hex
        fpath = base / user.name / _id / f'data.csv'
        fpath.parent.mkdir(parents=True, exist_ok=True)
        X.to_csv(fpath, index=False)
        td = TabularData(
            _id,
            f'{user.name}/{_id}/data.csv',
            user.id,
            name=dataset.name,
            target=dataset.default_target_attribute,
            meta={'openml_did': did},
        )
        request('data', asdict(td))
        user = User(user.id, user.name, data=user.data + [td.id] if user.data else [td.id])
        request('users', asdict(user))
        X.to_csv(fpath, index=False)
        print(did, i + 1, n)
    return


if __name__ == "__main__":
    import asyncio
    loop = asyncio.new_event_loop()
    loop.run_until_complete(main())
