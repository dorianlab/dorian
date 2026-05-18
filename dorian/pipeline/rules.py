from typing import Sequence, Any
from dataclasses import asdict

from dorian.dag import DAG, Node, Edge, ID, wildcard
from dorian.languages import PYTHON
from dorian.code.parsing.rule import (
    RewriteRule,
    Add,
    Apply,
    Delete,
    Revert,
    # ToOperator,
    # ToParameter,
    PurgeMode,
)

Rules = Sequence[RewriteRule]


def get_rules() -> Rules:
    single_character = r"^.$"
    basic = r"string|integer|float|identifier"
    types_to_delete = [
        # "module",
        # "expression_statement",
        "comment",
        "string_start",
        "string_end",
        "string_content",
        "from",
        # "import",
        "as",
    ]

    def _update(dag: DAG, key: ID, part: str, value: Any) -> DAG:
        # replaces a node with a new node, having the ID=key and the attribute part=value updated with the new value.
        if key not in dag.nodes: return dag
        node = Node(**dict(asdict(dag.nodes[key]), **{part: value}))
        return DAG(nodes=dict(dag.nodes, **{key: node}), edges=dag.edges)

    return [
        RewriteRule(
            description=f'Deletes nodes with types {", ".join(types_to_delete)}, in isolation',
            pattern=DAG(
                nodes={
                    "0": Node(
                        type=r"|".join(types_to_delete),
                        language=PYTHON,
                    )
                },
                edges=[],
            ),
            transformations=[Delete(nodes=["0"])],
        ),
        RewriteRule(
            description='Removes the children of an attribute, which themselves are attributes.',
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="list", # |attribute
                        language=PYTHON,
                    ),
                    "1": Node(
                        type=basic,
                        language=PYTHON
                    ),
                },
                edges=[Edge(source="0", destination="1")],
            ),
            transformations=[Delete(nodes=["1"])],
        ),
        RewriteRule(
            description="""assigns the type attribute of the 0 Node to the type value of 2 Node and then deletes 1,2 Nodes.
            The point is to delete two nodes and change the type value of the root from 'unary_operator' to 'identifier'""",
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="unary_operator",
                        language=PYTHON,
                    ),
                    "1": Node(language=PYTHON),
                    "2": Node(language=PYTHON),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[
                Apply(f=lambda g, m: _update(g, m["0"], "type", g.nodes[m["2"]].type)),
                Delete(nodes=["1", "2"], mode=PurgeMode.recursive),
            ],
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type=single_character,
                        text=single_character,
                        language=PYTHON,
                    )
                },
                edges=[],
            ),
            transformations=[Delete(nodes=["0"])],
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="dotted_name",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier",
                        language=PYTHON,
                    ), 
                },
                edges=[
                    Edge(source="0", destination="1")
                ]
            ),
            transformations=[
                Delete(nodes=["1"]),
            ],
        ),
        RewriteRule(
            description="""Handling import statements in 3 stages.
            This rule replaces aliased imports with their full path in the code.""",
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="aliased_import",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="dotted_name",
                        language=PYTHON,
                    ),
                    "2": Node(
                        type="identifier",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[
                Apply(lambda g, m: _update(g, m["1"], "type", "identifier")),
                Delete(nodes=["0", "2"]),
            ],
            rules=[
                lambda g, m: RewriteRule(
                    pattern=DAG(
                        nodes={
                            "0": Node(
                                type="attribute",
                                language=PYTHON,
                            ),          
                            "1": Node(
                                type="identifier",
                                text=g.nodes[m["2"]].text,
                                language=PYTHON,
                            ),
                        },
                        edges=[
                            Edge(source="0", destination="1")
                        ]
                    ),
                    transformations=[
                        Apply(
                            f=lambda _g, _m: _update(
                                _g,
                                _m["0"],
                                "text",
                                g.nodes[m["1"]].text
                            )
                        ),
                        Delete(nodes=["1"])
                    ]
                )
            ]
        ),
        # TODO: this rule doesn't handle the combination of "from" and "aliased" imports
        # Also, here the only difference between "1" and "2" node is the order of them. Can lead to errors.
        RewriteRule(
            description='changes the path of imported libraries to their complete path',
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="import_statement|import_from_statement",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="dotted_name",
                        language=PYTHON,
                    ),
                    "2": Node(
                        type="identifier|dotted_name",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[Delete(nodes=["2"])],
            rules=[
                lambda g, m: RewriteRule(
                    pattern=DAG(
                        nodes={
                            "0": Node(
                                type="call",
                                language=PYTHON,
                            ),
                            "1": Node(
                                type="attribute",
                                language=PYTHON,
                            ),
                            "2": Node(
                                type="identifier",
                                text=g.nodes[m["2"]].text,
                                language=PYTHON,
                            ),
                        },
                        edges=[
                            Edge(source="0", destination="1"),
                            Edge(source="1", destination="2"),
                        ]
                    ),
                    transformations=[
                        Apply(
                            f=lambda _g, _m: _update(
                                _g,
                                _m["1"],
                                "text",
                                f'{g.nodes[m["1"]].text}.{_g.nodes[_m["2"]].text}',
                            )
                        ),
                        Delete(nodes=["2"])
                    ]
                )
            ]
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="import_statement|import_from_statement",
                        language=PYTHON,
                    ),
                },
            ),
            transformations=[
                Delete(nodes=["0"], mode=PurgeMode.recursive)
            ]
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="attribute",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1")
                ]
            ),
            transformations=[
                Apply(f=lambda g, m: _update(g, m["0"], "text", f'{g.nodes[m["0"]].text}.{g.nodes[m["1"]].text}')),
                Delete(nodes=["1"])
            ]
        ),
        RewriteRule(
            description='handles keyword arguments.',
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="keyword_argument",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier",
                        language=PYTHON,
                    ),
                    "2": Node(language=PYTHON),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[
                ToParameter(nid="0", kw="1", value="2"),
                Delete(nodes=["1", "2"])
            ],
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="call",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="argument_list",
                        language=PYTHON,
                    ),
                    "2": Node(language=PYTHON),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="1", destination="2"),
                ]
            ),
            transformations=[
                Revert(nodes=["1", "2"])
            ]
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="call",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="argument_list",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                ]
            ),
            transformations=[
                Revert(nodes=["0", "1"]),
                Delete(nodes=["1"])
            ]
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="call",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="attribute",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                ]
            ),
            transformations=[
                ToOperator(nid="0", content="1"),
                Delete(nodes=["1"])
            ]
        ),
        RewriteRule(
            description='Transforms slicing/indexing to function calling',
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="subscript",
                        text=wildcard,
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier",
                        text=wildcard,
                        language=PYTHON,
                    ),
                    "2": Node(
                        type=r"identifier|int|string",
                        text=wildcard,
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[
                Revert(nodes=["0", "1", "2"])
            ],
        ),
        # !!! R8
        # Description: When method call exists, separates the class identifier from call function
        # Note: this rule must always follow R7!
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="call",
        #                 text=r"^[^(]+\..+$",
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[],
        #     ),
        #     transformations=[
        #         Apply(handle_method_call),
        #     ],
        # ),
        # !!! R9
        # attribute is names separated by dots when not used in import statements, like pandas.x.y.z
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="dictionary",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type="pair", text=wildcard, language=PYTHON
        #             ),
        #             # '2': Node(type=basic, text=wildcard, language=PYTHON),
        #             # '3': Node(type=wildcard, text=wildcard, language=PYTHON)
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             # Edge(source='1', destination='2'),
        #             # Edge(source='1', destination='3'),
        #         ],
        #     ),
        #     transformations=[
        #         # ToDictionary(nid='0')
        #         # ToOperator(nid='0', name='dict', language=Where('1', 'language')),
        #         # ToParameter(nid='1', name=Where('2', ''), type=, value=Where('')),
        #         # Add(edges=[('0', '1')]),
        #         Delete(nodes=["1"], edges=[("0", "1")], mode=PurgeMode.recursive)
        #     ],
        # ),
        # !!! R11
        # Description: Deletes print function instances
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="call",
        #                 text="^print\(.*\)$",
        #                 language=PYTHON,
        #             )
        #         },
        #         edges=[],
        #     ),
        #     transformations=[Delete(nodes=["0"], mode=PurgeMode.recursive)],
        # ),
        # !!! R13 (2 stage)
        # Description: connects the arguments of a function to its name/identifier/attribute
        # Note: takes too long. Better to be positioned after making the graph simpler.
        # It seems, it's better to add connections first and do the cleaning at the end.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type=r"call|method_call",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type=r"attribute|identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="argument_list",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type=r"string|keyword_argument|identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #             Edge(source="2", destination="3"),
        #         ],
        #     ),
        #     transformations=[
        #         Add(edges=[("3", "1")]),
        #         Delete(edges=[("2", "3")]),
        #     ],
        # ),
        # !!! R14
        # Description: Continuation of the last rule. Does the cleanup and renaming.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type=r"call|method_call",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type=r"attribute|identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="argument_list",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #         ],
        #     ),
        #     transformations=[
        #         # changes the type of attribute|identifier to call.
        #         Apply(
        #             f=lambda g, m: _update(
        #                 g,
        #                 m["1"],
        #                 "type",
        #                 "call",
        #             )
        #         ),
        #         Delete(nodes=["0", "2"]),
        #     ],
        # ),
        # !!! R15 (3 stages)
        # Description: handles list and tuple creation, with just considering one level nested lists or tuples.
        # First, mark the nested lists/tuples
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type=r"list|tuple",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type=r"list|tuple",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[Edge(source="0", destination="1")],
        #     ),
        #     transformations=[
        #         Apply(
        #             lambda g, m: _update(
        #                 g, m["0"], "type", f"nested_{g.nodes[m['0']].type}"
        #             )
        #         )
        #     ],
        # ),
        # !!! R18 (2 stage)
        # Description: Here, when we have a statement like x=12, then whenever we have x in another part of the code,
        # it connects 12 to it. Then at the end, it removes the nodes for assignment expression x=12.
        # Question: if both sides of the assignment are identifiers (like x=y) then the pattern can get mixed.
        # Note: this rule should always follow R13/R14
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(
                        type="expression_statement",
                        language=PYTHON,
                    ),
                    "1": Node(
                        type="identifier|pattern_list",
                        language=PYTHON,
                    ),
                    "2": Node(language=PYTHON),
                },
                edges=[
                    Edge(source="0", destination="1"),
                    Edge(source="0", destination="2"),
                ],
            ),
            transformations=[
                Add(edges=[("2", "1")]),
                Delete(nodes=["0"])
            ]
            # rules=[
            #     lambda g, m: RewriteRule(
            #         pattern=DAG(
            #             nodes={
            #                 "0": Node(
            #                     type="identifier",
            #                     text=g.nodes[m["2"]].text,
            #                     language=PYTHON,
            #                 ),
            #             },
            #             edges=[]
            #         ),
            #         transformations=[
            #             Add(edges=[(m["3"], "0")]),
            #             Delete(nodes=["0"])
            #         ],
            #     )
            # ],
        ),
        RewriteRule(
            pattern=DAG(
                nodes={
                    "0": Node(language=PYTHON,),
                    "1": Node(
                        type="identifier",
                        language=PYTHON,
                    ),
                },
                edges=[
                    Edge(source="0", destination="1"),
                ],
            ),
            transformations=[
                Delete(nodes=["1"])
            ],
            rules=[
                lambda g, m: RewriteRule(
                    pattern=DAG(
                        nodes={
                            "0": Node(
                                type="identifier",
                                text=g.nodes[m["1"]].text,
                                language=PYTHON,
                            ),
                        },
                        edges=[]
                    ),
                    transformations=[
                        Add(edges=[(m["0"], "0")]),
                        Delete(nodes=["0"])
                    ],
                )
            ],
        ),
        # !!! R19
        # Description: continuation of the last rule. Removes the assignment occurrences.
        # Question: if both sides are identifiers then the pattern can get mixed.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "1": Node(
        #                 type="assignment",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type=wildcard,
        #                 text=wildcard,
        #                 language=PYTHON
        #             ),
        #         },
        #         edges=[
        #             Edge(source="1", destination="2"),
        #             Edge(source="1", destination="3"),
        #         ],
        #     ),
        #     transformations=[
        #         Delete(nodes=["1", "2"], mode=PurgeMode.isolated),
        #     ],
        # ),
        # !!! R20 (2 stage)
        # Description: Exactly as the last two rules. But for pattern-list type.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "1": Node(
        #                 type="assignment",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="pattern_list",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "4": Node(
        #                 type=wildcard,
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="1", destination="2"),
        #             Edge(source="2", destination="3"),
        #             Edge(source="1", destination="4"),
        #         ],
        #     ),
        #     rules=[
        #         lambda g, m: RewriteRule(
        #             pattern=DAG(
        #                 nodes={
        #                     "0": Node(
        #                         type="identifier",
        #                         text=g.nodes[m["3"]],
        #                         language=PYTHON,
        #                     ),
        #                 },
        #             ),
        #             transformations=[Add(edges=[(m["4"], "0")]), Delete(nodes=["0"])],
        #         )
        #     ]
        # ),
        # !!! R21
        # Description: continuation of the last rule. Removes the assignment occurrences.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "1": Node(
        #                 type="assignment",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="pattern_list",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type=wildcard, text=wildcard, language=PYTHON
        #             ),
        #         },
        #         edges=[
        #             Edge(source="1", destination="2"),
        #             Edge(source="1", destination="3"),
        #         ],
        #     ),
        #     transformations=[
        #         Delete(nodes=["1"], mode=PurgeMode.isolated),
        #         Delete(nodes=["2"], mode=PurgeMode.recursive),
        #     ],
        # ),
        # # !!! R
        # # Question:
        # # At the end of transformations, we will have a triangle, root, left Wildcard, right Operator, with Wildcard connected to Operator.
        # # I don't find anywhere in the code having attribute node connected to keyword_argument node?
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="attribute",
        #                 text="sklearn.pipeline.Pipeline",
        #                 language=PYTHON,
        #             ),
        #             "1": Wildcard(type="Operator"),
        #             "2": Node(
        #                 type="keyword_argument",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "4": Node(
        #                 type="attribute",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #             Edge(source="2", destination="3"),
        #             Edge(source="2", destination="4"),
        #         ],
        #     ),
        #     transformations=[
        #         Delete(nodes=["2", "3"]),
        #         ToOperator(
        #             nid="4", name=Where("4", "text"), language=Where("4", "language")
        #         ),
        #         Add(edges=[("1", "4")]),
        #     ],
        # ),
        # # !!! R
        # # Question:
        # # comments: same as last rule
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="call", text=wildcard, language=PYTHON
        #             ),
        #             "1": Node(
        #                 type="attribute",
        #                 text="sklearn.pipeline.Pipeline",
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type="keyword_argument",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "3": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "4": Node(
        #                 type="attribute",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #             Edge(source="2", destination="3"),
        #             Edge(source="2", destination="4"),
        #         ],
        #     ),
        #     transformations=[
        #         Delete(nodes=["0", "2", "3"]),
        #         ToOperator(
        #             nid="4", name=Where("4", "text"), language=Where("4", "language")
        #         ),
        #         Add(edges=[("1", "4")]),
        #     ],
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="attribute",
        #                 text="sklearn.pipeline.Pipeline",
        #                 language=PYTHON,
        #             ),
        #             "1": Wildcard(type="Operator"),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #         ],
        #     ),
        #     transformations=[
        #         Delete(nodes=["0"]),
        #     ],
        # ),
        # # !!! R
        # # Description: Changes the call node to an Operator, with information from the 1 Node, which shows which function is being called
        # # Then removes the attribute/identifier node.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="call", text=wildcard, language=PYTHON
        #             ),
        #             "1": Node(
        #                 type="attribute|identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #         ],
        #     ),
        #     transformations=[
        #         ToOperator(
        #             nid="0", name=Where("1", "text"), language=Where("1", "language")
        #         ),
        #         Delete(nodes=["1"]),
        #     ],
        # ),
        # # !!! R
        # # Question:
        # # Vague. Here we are transforming the argument to a parameter, and we are getting it's name, type and value
        # # from the 2 node, which is a Wildcard. The type here specially is Operator, which is not consistent with the types
        # # we expect for a Parameter, like int, string, float.
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="keyword_argument",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Wildcard(type="Operator"),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #         ],
        #     ),
        #     transformations=[
        #         ToParameter(
        #             nid="0",
        #             name=Where("1", "text"),
        #             type=Where("2", "type"),
        #             value=Where("2", "text"),
        #         ),
        #         Delete(nodes=["1"]),
        #     ],
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             "0": Node(
        #                 type="keyword_argument",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "1": Node(
        #                 type="identifier",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #             "2": Node(
        #                 type=basic + "|list|attribute",
        #                 text=wildcard,
        #                 language=PYTHON,
        #             ),
        #         },
        #         edges=[
        #             Edge(source="0", destination="1"),
        #             Edge(source="0", destination="2"),
        #         ],
        #     ),
        #     transformations=[
        #         ToParameter(
        #             nid="0",
        #             name=Where("1", "text"),
        #             type=Where("2", "type"),
        #             value=Where("2", "text"),
        #         ),
        #         Delete(nodes=["1", "2"]),
        #     ],
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Wildcard(type='Operator'),
        #             '1': Wildcard(type='Parameter')
        #         },
        #         edges=[
        #             Edge(source='0', destination='1'),
        #         ]
        #     ),
        #     transformations=[
        #         Flip(edges=[('0', '1')])
        #     ]
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='dictionary', text=wildcard, language=PYTHON),
        #             '1': Node(type='pair', text=wildcard, language=PYTHON),
        #             '2': Node(type=basic, text=wildcard, language=PYTHON),
        #             '3': Node(type=wildcard, text=wildcard, language=PYTHON)
        #         },
        #         edges=[
        #             Edge(source='0', destination='1'),
        #             Edge(source='1', destination='2'),
        #             Edge(source='1', destination='3'),
        #         ]
        #     ),
        #     transformations=[
        #         Delete(nodes=['1'], edges=[('0', '1'), ('1', '2'), ('1', '3')]),
        #         Add(edges=[('2', '3')]),
        #     ]
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='call', text=wildcard, language=PYTHON),
        #             '1': Node(type='attribute', text=wildcard, language=PYTHON),
        #             '2': Node(type='argument_list', text=wildcard, language=PYTHON),
        #         },
        #         edges=[
        #             Edge(source='0', destination='1'),
        #             Edge(source='0', destination='2')
        #         ]
        #     ),
        #     transformations=[
        #         Delete(nodes=['0', '2'], edges=[('0', '1'), ('0', '2')]),
        #     ]
        # ),
        # # !!! R
        # RewriteRule(
        #     pattern=DAG(
        #         nodes={
        #             '0': Node(type='module|expression_statement', text=wildcard, language=PYTHON),
        #         },
        #         edges=[]
        #     ),
        #     transformations=[Delete(nodes=['0'])]
        # ),
    ]