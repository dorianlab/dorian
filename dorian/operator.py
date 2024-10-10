from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import importlib
import traceback
import json
import os

from dorian.languages import SupportedLanguage
import dorian.dag as dag

from backend.events import emit, Event
from backend.config import base


@dataclass
class Variable:
    name: str


known = defaultdict(lambda: 'unknown', {
    'pandas.read_csv': 'function',
    'pandas.DataFrame.columns': 'method',
    'fit': 'method',
    'predict': 'method',
    'transform': 'method',
    'fit_transform': 'method',
    'sklearn.preprocessing.MinMaxScaler': 'sklearn-preprocessor',
    'sklearn.model_selection.train_test_split': 'function',
    'sklearn.linear_model.LinearRegression': 'sklearn-estimator',
    'sklearn.metrics.mean_squared_error': 'function',
})


@dataclass
class Operator:
    name: str | Variable
    language: SupportedLanguage

    def __hash__(self) -> int:
        return hash(f'{self.language}:{self.name}')

    def __call__(self, *args, **kwargs):     
        match known[self.name]:
            case 'function' | 'class' | 'sklearn-estimator' | 'sklearn-preprocessor':
                module, method = self.name.rsplit('.', 1)
                version = matching_version(self)
                path = primitive(self) / f'{version}/{self.name}.json'
                
                if path.exists():
                    with open(path, 'r') as f:
                        meta = json.load(f)
                    # match (meta
                    #        .get('classifiers', {'type': f'key "classifiers" not found'})
                    #        .get('type', f'key "classifiers:type" not found')):
                    #     case 'preprocessor':
                    #         pass
                    #     case 'estimator':
                    #         pass
                    #     case other:
                    #         info = {
                    #             'type': 'WrongPrimitive',
                    #             'error': f'Primitive cannot be handled, {other}',
                    #             'operator': self.name,
                    #             'language': self.language
                    #         }
                    #         emit(Event("Exception", info))    
                else:
                    info = {
                        'type': 'UnknownOperator',
                        'error': f'Primitive not found, {path}',
                        'operator': self.name,
                        'language': self.language
                    }
                    emit(Event("Exception", info))

                if importlib.util.find_spec(module):
                    module = importlib.import_module(module)
                    return getattr(module, method)(*args, **kwargs)
                else:
                    try:
                        return eval(self.name)
                    except:
                        info = {
                            'type': 'UnknownOperator',
                            'error': traceback.format_exc().splitlines()[-1],
                            'operator': self.name,
                            'language': self.language
                        }
                        emit(Event("Exception", info))
            case 'method':
                obj, *args = args
                res = getattr(obj, self.name)(*args, **kwargs)
                return res if self.name != 'fit' else obj
            # case 'sklearn-preprocessor':
            #     return getattr(module, method)().fit_transform(*args, **kwargs)
            case 'unknown':
                info = {
                    'type': 'UnknownOperatorType',
                    'error': 'extend the operators knowledge base, dorian.operator.known',
                    'operator': self.name,
                    'language': self.language
                }
                emit(Event("Exception", info))
            case other:
                info = {
                    'type': 'WrongOperatorType',
                    'error': f'type "{other}"',
                    'operator': self.name,
                    'language': self.language
                }
                emit(Event("Exception", info))


def primitive(operator: Operator) -> Path:
    return base / f'dorian/knowledge/collection/data/{operator.language}/{operator.name.split(".")[0]}/'


class DefaultKeyDict(dict):
    def __getitem__(self, key):
        return dict.get(self, key, key)


libraries = DefaultKeyDict({
    'sklearn': 'scikit-learn',
})


def matching_version(operator: Operator) -> str:
    module, method = operator.name.rsplit('.', 1)
    try:
        library  = libraries[module.split('.')[0]]
        version = importlib.metadata.version(library)
        matches = sorted(primitive(operator).glob('*'), key=lambda v: -len(os.path.commonprefix([version, v.name])))
        return matches[0].name if matches else 'unknown'
    except importlib.metadata.PackageNotFoundError:
        # for distr in sorted(importlib.metadata.distributions(), key=lambda d: d.name):
        #     print(distr.name, distr.version)
        info = {
            'type': 'LibraryNotFound',
            'error': traceback.format_exc().splitlines()[-1],
            'operator': operator.name,
            'language': operator.language
        }
        emit(Event('Exception', info))
        version = 'unknown'