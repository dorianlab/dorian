from dorian.code.parsing.rule import RewriteRule, Apply
from dorian.dag import DAG, Node
from backend.events import Event
from .utils.debugger import populate_tasks  # contains populate_tasks()

rules = [
    RewriteRule(
        description="Fetches the data science task annotation from KG",
        pattern=DAG(
            nodes={ '0': Node(type='Operator') },
        ),
        emit=lambda g, m: [
            Event('OperatorFound', {'name': g.nodes[m['0']].name, 'nid': m['0']})
        ],
        transformations=[Apply(populate_tasks)],
    )
]
