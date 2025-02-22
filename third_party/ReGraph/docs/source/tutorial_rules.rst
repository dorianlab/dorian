.. _tutorial_rules:

========================
Rewriting rules tutorial
========================

In the context of ReGraph, by rewriting rules we mean the rules of *sesqui-pushout rewriting* (see more details `here <https://ncatlab.org/nlab/show/span+rewriting#sesquipushout_rewriting/>`_). A rewriting rule consists of the three graphs: `P` – preserved part, `LHS` – left hand side, `RHS` – right hand side, and two mappings: from `P` to `LHS` and from `P` to `RHS`.

Informally, `LHS` represents a pattern to match in a graph, subject to rewriting. `P` together with the mapping `P -> LHS` specifies a part of the pattern which stays preseved during rewriting, i.e. all the nodes/edges/attributes present in `LHS` but not `P` will be removed. `RHS` and `P -> RHS` specify nodes/edges/attributes to add to the `P`. In addition, rules defined is such a way allow to clone and merge nodes. If two nodes from `P` map to the same node in `LHS`, the node corresponding to this node of the pattern will be cloned. Symmetrically, if two nodes from `P` map to the same node in $rhs$, the corresponding two nodes will be merged.

This tutorial will illustrate the idea behind the sesqui-pushout rewriting rules in more detail.

Let us start by importing the necessary data structures and functions:

::

    from regraph import NXGraph, Rule, plot_rule

.. _tutorial_rules1:

----------------------------------------
Creating a rewriting rule from a pattern
----------------------------------------

::

    # Define the left-hand side of the rule
    pattern = NXGraph()
    pattern.add_nodes_from([1, 2, 3])
    pattern.add_edges_from([(1, 2), (2, 3)])

    rule1 = Rule.from_transform(pattern)
    # `inject_clone_node` returns the IDs of the newly created
    # clone in P and RHS
    p_clone, rhs_clone = rule1.inject_clone_node(1)
    rule1.inject_add_node("new_node")
    rule1.inject_add_edge("new_node", rhs_clone)


Now, let us plot the rule

>>> plot_rule(rule1)

.. image:: _static/rules/r1.png

Every rule can be converted to a sequence of human-readable commands:

>>> print(rule1.to_commands())
CLONE 1 AS 11.
ADD_NODE new_node {}.
ADD_EDGE new_node 11 {}.


.. _tutorial_rules2:

---------------------------------------------------
Creating a rewriting rule from a span
---------------------------------------------------


By default, `Rule` objects in ReGraph are initialized with three graph objects (`NXGraph`) corresponding to `P`, `LHS` and `RHS`, together with two Python dictionaries encoding the homomorphisms `P -> LHS` and `P -> RHS`. This may be useful in a lot of different scenarios. For instance, as in the following example:

::

    # Define the left-hand side of the rule
    pattern = NXGraph()
    pattern.add_nodes_from([1, 2, 3])
    pattern.add_edges_from([(1, 2), (1, 3), (1, 1), (2, 3)])

    # Define the preserved part of the rule
    rule2 = Rule.from_transform(pattern)
    p_clone, rhs_clone = rule2.inject_clone_node(1)


>>> plot_rule(rule2)

.. image:: _static/rules/r2.png


>>> print("New node corresponding to the clone: ", p_clone)
New node corresponding to the clone:  11
>>> print(rule2.p.edges())
[(1, 2), (1, 3), (1, 1), (1, '11'), (2, 3), ('11', 1), ('11', 3), ('11', '11'), ('11', 2)]


As the result of cloning of the node `1`, all its incident edges are copied to the newly created clone node (variable `p_clone`). However, in our rule we would like to keep only some of the edges and remove the rest as follows.

rule2.inject_remove_edge(1, 1)
rule2.inject_remove_edge(p_clone, p_clone)
rule2.inject_remove_edge(p_clone, 1)
rule2.inject_remove_edge(p_clone, 2)
rule2.inject_remove_edge(1, 3)

>>> print(rule2.p.edges())
[(1, 2), (1, '11'), (2, 3), ('11', 3)]
>>> plot_rule(rule2)

.. image:: _static/rules/r3.png

Instead of initializing our rule from the pattern and injecting a lot of edge removals, we could directly initialize three objects for `P`, `LHS` and `RHS`, where `P` contains only the desired edges. In the following example, because the rule does not specify any merges or additions (so `RHS` is isomorphic to `P`), we can omit the parameter `RHS` in the constructor of `Rule`.

::

    # Define the left-hand side of the rule
    lhs = NXGraph()
    lhs.add_nodes_from([1, 2, 3])
    lhs.add_edges_from([(1, 2), (1, 3), (1, 1), (2, 3)])

    # Define the preserved part of the rule
    p = NXGraph()
    p.add_nodes_from([1, "1_clone", 2, 3])
    p.add_edges_from([
        (1, 2),
        (1, "1_clone"),
        ("1_clone", 3),
        (2, 3)])

    p_lhs = {1: 1, "1_clone": 1, 2: 2, 3: 3}

    # Initialize a rule object
    rule3 = Rule(p, lhs, p_lhs=p_lhs)


>>> plot_rule(rule3)

.. image:: _static/rules/r3.png


>>> print(rule3.p.edges())
[(1, 2), (1, '1_clone'), ('1_clone', 3), (2, 3)]


--------
See more
--------

Module reference: :ref:`rules`
