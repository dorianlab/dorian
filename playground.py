from backend.events import Event, subscribe
from backend.envs import executor, submit, close
from dorian.dag import DAG, Edge, Operator, Snippet, Parameter
from dorian.pipeline.execution import execute


def handle_exceptions(exc: Event):
    match exc.data['type']:
        case 'LibraryNotFound': # Maybe switch to classes instead of str (for better validation)
            print(exc.data)
        case _:
            print(exc.data)
    return


subscribe("Exception", handle_exceptions)


projection = """
def foo(df):
    features = ["median_income", "housing_median_age", "total_rooms"]
    target = "median_house_value"
        
    X = df[features]
    y = df[target]
    return X, y
"""
printing = """
def foo(*args):
    return ", ".join(map(str, args))
"""

pipeline = DAG(
    nodes={
        'fname': Parameter('fname', 'str', 'data/housing.csv'),
        'data_loading': Operator('pandas.read_csv', language='python'),
        'preprocessing': Snippet(projection, language='python'),
        'scaling': Operator('sklearn.preprocessing.MinMaxScaler', language='python'),
        'transform': Operator('fit_transform', language='python'),
        'split': Operator('sklearn.model_selection.train_test_split', language='python'),
        'model': Operator('sklearn.linear_model.LinearRegression', language='python'),
        'training': Operator('fit', language='python'),
        'prediction': Operator('predict', language='python'),
        'mse': Operator('sklearn.metrics.mean_squared_error', language='python'),
        'print': Snippet(printing, language='python'),
    },
    edges=[
        Edge('fname', 'data_loading', 0),
        Edge('data_loading', 'preprocessing', 0),
        Edge('scaling', 'transform', 0),
        Edge('preprocessing', 'transform', 1, 0),
        Edge('transform', 'split', 0, 0),
        Edge('preprocessing', 'split', 1, 1),
        Edge('model', 'training', 0),
        Edge('split', 'training', 1, 0),
        Edge('split', 'training', 2, 2),
        Edge('training', 'prediction', 0),
        Edge('split', 'prediction', 1, 1),
        Edge('split', 'mse', 0, 3),
        Edge('prediction', 'mse', 1),
        Edge('mse', 'print', 0)
    ])


if __name__=="__main__":
    future = submit(execute, pipeline)
    executor.gather(future)
    print(future.result())
    close()