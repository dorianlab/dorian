from collections import defaultdict
from pendulum import datetime, now
from dataclasses import dataclass
from typing import Any, Callable
from pytz import timezone, utc
import pandas as pd
import dataclasses
import json

from .envs import submit
from sqlalchemy import create_engine

tmz, time_fmt = 'Europe/Berlin', '%Y-%m-%d %H:%M:%S'
dbpath = "sqlite:///data/dorian.db"
db = create_engine(dbpath)


@dataclass
class Event:
    type: str
    data: Any = None


class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        if isinstance(o, pd.Timestamp | datetime):
            return timezone(tmz).localize(o, is_dst=None).astimezone(utc).strftime(time_fmt)
        return super().default(o)


def log(event: Event):
    df = pd.DataFrame({
        "timestamp": [now().to_datetime_string()],
        "event": [event.type],
        "data": [json.dumps(event.data, cls=EnhancedJSONEncoder)]
    })
    try:
        df.to_sql('logs', db, if_exists='append', index=False)
    except:
        df.to_csv('logs/events.log', mode='a', index=False)


def verbose(event: Event):
    print(f'{now().to_datetime_string()} {event.type} - {json.dumps(event.data, cls=EnhancedJSONEncoder)}')


handlers = defaultdict(lambda: [log, verbose])


def subscribe(event: str, fn: Callable):
    global handlers
    handlers[event].append(fn)


def emit(*events: Event):
    for event in events:
        for fn in handlers[event.type]:
            submit(fn, event)
