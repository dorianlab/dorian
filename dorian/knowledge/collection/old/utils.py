import networkx as nx
from networkx.drawing.nx_pydot import graphviz_layout
import matplotlib.pyplot as plt
import ast
import json
from regraph import NXGraph, Rule

USING_OPTIMIZED_MATCHING = True
COLLECTING_RULE_DATA = True
COLLECTING_SCRIPT_STAT = True

def get_root_node_id(G: NXGraph):
    root_node = 0
    return root_node


def create_graph_from_pattern(pattern):
    # check if pattern is a connected graph
    nodes = pattern.nodes()
    edges = pattern.edges()
    graph = nx.Graph()
    graph.add_nodes_from(nodes)
    graph.add_edges_from(edges)
    return graph


def pattern_connected(pattern: NXGraph):
    return nx.is_connected(create_graph_from_pattern(pattern))


def remove_nodes(G, ids):
    for id in ids:
        G.remove_node(id)
    return


# creates a pattern to filter a graph based on "node type"
# attr_name -> name given to the variable used to identify this node type
# node_type -> the type of node that wants to be filtered
def create_pattern(id, attr_name, node_type):
    pattern = NXGraph()
    pattern.add_node(id, {attr_name: node_type})
    return pattern


def get_ancestors_nodes(G: NXGraph, node_id):
    subg_nodes = list(G.ancestors(node_id))
    subg_nodes.append(node_id)
    return subg_nodes


def create_subgraph(G, node_id):
    subg_nodes = list(G.descendants(node_id))
    subg_nodes.append(node_id)
    subgraph = G.generate_subgraph(G, subg_nodes)
    return subgraph


def print_graph(G):
    # print clear view of all nodes and their edges
    print("List of nodes: ")
    for n, attrs in G.nodes(data=True):
        print("\t", n, attrs)
        continue
        if 'text' in attrs.keys():
            print(f"{n} type:'{attrs['type']}', text:'{attrs['text']}'")
        else:
            print(f"{n} type:'{attrs['type']}'")
    print()
    print("List of edges: ")
    for s, t, attrs in G.edges(data=True):
        print("\t{}->{}".format(s, t), attrs)
    print()


def print_nodes(graph, node_ids):
    for id in node_ids:
        print(graph.get_node(id))


def convert_nxgraph_to_graph(NXGraph):
    """
    Extracts and nx.Graph from the NXGraph
    :param NXGraph
    :return: nx.Graph
    """
    nxGraph = nx.Graph(NXGraph._graph)
    return nxGraph


def read_rule_from_string(line):
    string = line.rstrip()
    rule = ast.literal_eval(string)
    return rule


def get_ascendant_subgraphs_by_pattern(G: NXGraph, pattern: NXGraph):
    """
    Finds a complete subgraph of ascendants for a given pattern.

    :param G: an NXGraph object
    :param pattern: pattern to find ascendants of
    :return: found subgraphs
    """
    anti_roots = [node for node in pattern.nodes() if len(pattern.descendants(node)) == 0]
    if len(anti_roots) != 1:
        raise Exception("Pattern has more than one root")
    anti_root_id = anti_roots[0]
    anti_root_attrs = pattern.get_node(anti_root_id)
    # find all instances of root in graph
    root_pattern = NXGraph()
    root_pattern.add_node(anti_root_id, anti_root_attrs)
    instances = G.find_matching(root_pattern)
    subgraphs = []
    if instances:
        for instance in instances:
            found_subgraph = get_ancestors_nodes(G, list(instance.values())[0])
            if len(found_subgraph) < len(pattern.nodes()):
                continue
            duplicate = False
            subgraphs_to_delete = list()
            for subgraph in subgraphs:
                if set(found_subgraph).issubset(set(subgraph)):
                    duplicate = True
                    continue
                if set(subgraph).issubset(set(found_subgraph)):
                    subgraphs_to_delete.append(subgraph)
            for subgraph_to_delete in subgraphs_to_delete:
                subgraphs.remove(subgraph_to_delete)
            if not duplicate:
                subgraphs.append(found_subgraph)
    return subgraphs


def nxraph_to_digraph(nxgraph: NXGraph):
    digraph = nx.DiGraph()
    for node_id, node_attrs in nxgraph.nodes(data=True):
        digraph.add_nodes_from([(node_id, {name: attr}) for (name, attr) in node_attrs.items()])
    for s, t, attrs in nxgraph.edges(data=True):
        if len(attrs.items()) != 0:
            digraph.add_edges_from([(s, t, {name: attr}) for (name, attr) in attrs.items()])
        else:
            digraph.add_edge(s, t)
    # print_graph(digraph)
    return digraph


def draw_graph_by_attr(G, attribute="type", id=False, fig_num=1):
    fig = plt.figure(fig_num)
    if type(G) is NXGraph:
        G = nxraph_to_digraph(G)
    # set graph structure to tree
    pos = graphviz_layout(G, prog="dot")
    width = 2
    plt.figure(figsize=(8, 5))
    if not id:
        labels = {}
        ids = G.nodes()
        labels_type = nx.get_node_attributes(G, attribute)
        # transform labels from finite set to strings
        for id, k in zip(ids, labels_type): #):
            for ttype in labels_type[k]: # ,
                labels[k] = str(str(id) + "\n" + ttype ) #  .replace("'", "") str(id) + "\n" +  + "\n" + text.replace("'", "")
                #else:
                    #labels[k] = str(id, "\n",  value.decode("utf-8").replace("'", ""))
        nx.draw(G, pos=pos,
                with_labels=True,
                node_color="lightgrey",
                edge_color="lightgrey",
                labels=labels,
                alpha=0.9,
                width=width,
                node_size=2200,
                arrowsize=20)
    else:
        nx.draw(G, pos=pos,
                with_labels=True,
                node_color="lightgrey",
                edge_color="lightgrey",
                alpha=0.9,
                width=width,
                node_size=2200,
                arrowsize=20)
    plt.show()
    return fig


def draw_graph_rule(G, attribute="text", id=False, title='', figsize = (15,10), ax=None):
    if type(G) is NXGraph:
        G = nxraph_to_digraph(G)
    # set graph structure to tree
    pos = graphviz_layout(G, prog="dot")
    width = 2
    if ax:
        ax.set_title(title)
    else:
        plt.title(title)
        plt.figure(figsize=figsize)
    if not id:
        labels = {}
        ids = G.nodes()
        labels_type = nx.get_node_attributes(G, "type")
        labels_text = nx.get_node_attributes(G, "text")
        # transform labels from finite set to strings
        for id in ids:
            ttype = ''
            ttext = ''
            if id in labels_type.keys():
                ttype = next(iter(labels_type[id]))
            if id in labels_text.keys():
                ttext = next(iter(labels_text[id]))
            labels[id] = str(str(id) + "\n" + ttype + "\n" + ttext) #  .replace("'", "") str(id) + "\n" +  + "\n" + text.replace("'", "")
                #else:
                    #labels[k] = str(id, "\n",  value.decode("utf-8").replace("'", ""))
        nx.draw(G,
                ax=ax,
                pos=pos,
                with_labels=True,
                node_color="lightgrey",
                edge_color="lightgrey",
                labels=labels,
                alpha=0.9,
                width=width,
                node_size=2200,
                arrowsize=20)
    else:
        nx.draw(G,
                ax=ax,
                pos=pos,
                with_labels=True,
                node_color="lightgrey",
                edge_color="lightgrey",
                alpha=0.9,
                width=width,
                node_size=2200,
                arrowsize=20)
    if not ax:
        plt.show()
    return


def draw_graph(G, attribute="text", id=False, title='', figsize = (20,10)):
    if type(G) is NXGraph:
        G = nxraph_to_digraph(G)
    # set graph structure to tree
    pos = graphviz_layout(G, prog="dot")
    width = 2
    plt.figure(figsize=figsize)
    plt.title(title)
    if not id:
        labels = {}
        ids = G.nodes()
        labels_type = nx.get_node_attributes(G, "type")
        labels_text = nx.get_node_attributes(G, "text")
        # transform labels from finite set to strings
        for id in ids:
            ttype = ''
            ttext = ''
            if id in labels_type.keys():
                ttype = next(iter(labels_type[id]))
            if id in labels_text.keys():
                ttext = next(iter(labels_text[id]))
            labels[id] = str(str(id) + "\n" + ttype + "\n" + ttext) #  .replace("'", "") str(id) + "\n" +  + "\n" + text.replace("'", "")
                #else:
                    #labels[k] = str(id, "\n",  value.decode("utf-8").replace("'", ""))
        nx.draw(G,
                pos=pos,
                with_labels=True,
                node_color="lightgrey",
                edge_color="lightgrey",
                labels=labels,
                alpha=0.9,
                width=width,
                node_size=2200,
                arrowsize=20)
    else:
        nx.draw(G,
                pos=pos,
                with_labels=True,
                node_color="lightgrey",
                edge_color="lightgrey",
                alpha=0.9,
                width=width,
                node_size=2200,
                arrowsize=20)
    plt.show()
    return


def draw_rule(rule_dict, extractor):
    rule = Rule.from_json(rule_dict)
    pattern = rule.lhs
    result = extractor.get_transformation_result(pattern, rule_dict)
    fig, axes = plt.subplots(1,2, figsize=(20,10))
    pattern_fig = draw_graph_rule(pattern, title='Pattern', ax=axes[0])
    result_fig = draw_graph_rule(result, title='Result', ax=axes[1])
    plt.show()

#def draw_rule(rule):
 #   draw_graph(rule.lhs)
  #  draw_graph(rule.rhs)


def jsonify_finite_set(param):
    if type(param) !=  str:
        if len(param.to_json()["data"]) > 1:
            result_list = list()
            for element in param.to_json()["data"]:
                if isinstance(element, (bytes, bytearray)):
                    result_list.append(element.decode("utf-8"))
                else:
                    result_list.append(element)
            return result_list
        data = param.to_json()["data"][0]
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")
        return data
    else:
        return param


def draw_diffgraph(Gdiff, attribute="text"):
    # transform labels from finite set to strings
    if type(Gdiff) is NXGraph:
        Gdiff = nxraph_to_digraph(Gdiff)
    ids = Gdiff.nodes()
    labels = {}
    ids = Gdiff.nodes()
    labels_type = nx.get_node_attributes(Gdiff, "type")
    labels_text = nx.get_node_attributes(Gdiff, "text")
    # transform labels from finite set to strings
    for id, k, n in zip(ids, labels_type, labels_text):
        for ttype, text in zip(labels_type[k], labels_text[n]):
            labels[k] = str(str(id) + "\n" + ttype.replace("'", "") + "\n" + text.replace("'", ""))
    pos = graphviz_layout(Gdiff, prog="dot")
    # color different nodes depending on the labels
    val_map = {"both": "lightgrey",
               "G1": "red",
               "G2": "green",
               "updated": "lightblue"
               }
    node_colors = []
    for node, attrs in Gdiff.nodes(data=True):
        if len(attrs["origin"]) == 2:
            node_colors.append(val_map["both"])
        elif len(attrs["origin"]) == 1:
            for elem in attrs["origin"]:
                if elem == "G1":
                    node_colors.append(val_map["G1"])
                elif elem == "G2":
                    node_colors.append(val_map["G2"])
                elif elem == "updated":
                    node_colors.append(val_map["updated"])
    edge_colors = []
    for s, t, attrs in Gdiff.edges(data=True):
        if len(attrs["origin"]) == 2:
            edge_colors.append(val_map["both"])
        elif len(attrs["origin"]) == 1:
            for elem in attrs["origin"]:
                if elem == "G1":
                    edge_colors.append(val_map["G1"])
                elif elem == "G2":
                    edge_colors.append(val_map["G2"])
    # add labels=labels, for different labeling
    # add node_color='lightblue' for one colored nodes
    plt.figure(figsize= (20,10))
    nx.draw(Gdiff, pos=pos,
            with_labels=True,
            cmap=plt.get_cmap('inferno'),
            node_color=node_colors,
            edge_color=edge_colors,
            labels=labels,
            alpha=0.9,
            width=2,
            node_size=2200,
            arrowsize=20)
    plt.show()


def convert_graph_to_json(G: NXGraph):
    graph_dict = {"nodes": [], "edges": []}
    for n, attrs in G.nodes(data=True):
        if str(attrs["type"]) == "{'input'}":
            metadata = {"id": str(n), "type": "input", "targetPosition": "top", "position": {"x": 0, "y": 0},
                        "data": {}}
        else:
            metadata = {"id": str(n), "type": "default", "targetPosition": "top", "position": {"x": 0, "y": 0},
                        "data": {}}
        # node_attrs = {}
        node_raw_attrs = G.get_node(n)
        for key in node_raw_attrs:
            metadata["data"].update({key: jsonify_finite_set(node_raw_attrs[key])})

        # node_attrs.update(G.get_node(n))
        graph_data = {}
        graph_dict["nodes"].append(metadata)
        # graph_dict["nodes"].append(graph_data)
    i = 1
    for s, t, attrs in G.edges(data=True):
        edge_id = "edge-" + str(i)
        edge_attrs = {"id": edge_id, "source": str(s), "target": str(t), 'type': 'smoothstep'}
        graph_dict["edges"].append(edge_attrs)
        i += 1
    with open('../graph.json', 'w') as fp:
        json.dump(graph_dict, fp, indent=4)
    #print(graph_dict)
    return graph_dict


def convert_graph_to_json_new_frontend(G: NXGraph):
    graph_dict = {"nodes": [], "edges": []}
    conversion_maping = {
        "call": "operator",
        "keyword_argument": "hyperparameter",
        "input_variable": "data",
        "output_variable": "data",
        "passable_data": "data",
        "identifier": "data"
    }
    for n, attrs in G.nodes(data=True):
        type = str(attrs["type"]).replace("{'", "").replace("'}", "")
        if type not in conversion_maping.keys():
            #"targetPosition": "top", "position": {"x": 0, "y": 0},
            metadata = {"id": str(n), "type": "util",
                         "data": {}}
        else:
            # "targetPosition": "top","position": {"x": 0, "y": 0},
            metadata = {"id": str(n), "type": conversion_maping[type],
                        "data": {}}
        # node_attrs = {}
        node_raw_attrs = G.get_node(n)
        for key in node_raw_attrs:
            metadata["data"].update({key: jsonify_finite_set(node_raw_attrs[key])})

        # node_attrs.update(G.get_node(n))
        graph_data = {}
        graph_dict["nodes"].append(metadata)
        # graph_dict["nodes"].append(graph_data)
    i = 1
    for s, t, attrs in G.edges(data=True):
        #, 'type': 'smoothstep'
        edge_id = str(i)
        edge_attrs = {"id": edge_id, "source": str(s), "target": str(t)}
        graph_dict["edges"].append(edge_attrs)
        i += 1
    with open('../graph.json', 'w') as fp:
        json.dump(graph_dict, fp, indent=4)
    print(graph_dict)
    return graph_dict


def nxgraph_to_json(G):
    graph_dict = {"nodes": [], "edges": []}
    for n, attrs in G.nodes(data=True):
        json_attrs = {"id": n}
        for attr in attrs:
            json_attrs.update({attr: jsonify_finite_set(attrs[attr])})
        graph_dict["nodes"].append(json_attrs)
    for s, t in G.edges():
        edge_attrs = {"source": s, "target": t}
        graph_dict["edges"].append(edge_attrs)
    #print(graph_dict)
    return graph_dict



def json_to_nxgraph(graph_dict):
    graph = NXGraph()
    for node in graph_dict["nodes"]:
        node_id = node.pop("id")
        graph.add_node(node_id, node)
    for edge in graph_dict["edges"]:
        graph.add_edge(edge["source"], edge["target"])
    return graph


def json_to_nxgraph_for_gdiff(graph_dict):
    graph = NXGraph()
    for node in graph_dict["nodes"]:
        node_id = node.pop("id")
        node_attrs = {"type" : node["type"]}
        if "text" in node.keys():
            node_attrs["text"] = node["text"]
        graph.add_node(node_id, node_attrs)
    for edge in graph_dict["edges"]:
        graph.add_edge(edge["source"], edge["target"])
    return graph

