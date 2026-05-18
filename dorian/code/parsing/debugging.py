from collections import defaultdict
from typing import Sequence
from pendulum import now
from functools import wraps


profiling = defaultdict(list)


def verbose(function=None, types: Sequence[str] = []):
    def _decorate(function):

        @wraps(function)
        def wrapper(*args, **kwargs): 
            start = now()
            result = function(*args, **kwargs)  
            
            output = [f'Function {function.__name__}']
            for arg in args:
                if arg.__class__.__name__ in types:
                    output.extend([arg.__class__.__name__, repr(arg)])
            output.append(f'in {(now() - start).in_words()}.\n\n')
            print("\n".join(output))
            return result
    
        return wrapper

    if function: return _decorate(function)
    return _decorate


def summarize(function=None, types: Sequence[str] = [], origin: str = ''):
    def _decorate(function):

        @wraps(function)
        def wrapper(*args, **kwargs): 
            start = now()
            result = function(*args, **kwargs)  
            
            output = [f'{origin}{" " if origin else ""}{function.__name__}']
            for arg in args:
                # print(arg.__class__.__name__)
                if arg.__class__.__name__ in types:
                    output.extend([arg.__class__.__name__, repr(arg)])
            profiling["\n".join(output)].append(now() - start)
            return result
    
        return wrapper

    if function: return _decorate(function)
    return _decorate
