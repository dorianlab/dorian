"""Collection of tests for ReGraph_neo4j graphs."""
import warnings
from regraph import Neo4jGraph
from regraph.backends.neo4j.cypher_utils import *


class TestNeo4jGraph(object):

    @classmethod
    def setup_class(self):
        try:
            self.g = Neo4jGraph(uri="bolt://localhost:7687",
                                user="neo4j",
                                password="admin")
            self.g._clear()
            nodes = [
                ("a", {"name": "EGFR", "state": "p"}),
                ("b", {"name": "BND"}),
                ("c", {"name": "Grb2", "aa": "S", "loc": {90}}),
                ("d", {"name": "SH2"}),
                ("e", {"name": "EGFR"}),
                ("f", {"name": "BND"}),
                ("g", {"name": "Grb2"}),
                ("h", {"name": "WAF1"}),
                ("i", {"name": "BND"}),
                ("j", {"name": "G1-S/CDK", "state": "p"}),
                "k", "l", "m"
            ]
            edges = [
                ("a", "b", {"s": "p"}),
                ("d", "b", {"s": "u"}),
                ("d", "c"),
                ("e", "f", {"s": "p"}),
                ("g", "f", {"s": "u"}),
                ("e", "g", {"act": {2}}),
                ("g", "e", {"act": {1}}),
                ("h", "i"),
                ("i", "h"),
                ("j", "h", {"act": {1}}),
                ("j", "i", {"act": {2}}),
                ("k", "l"),
                ("l", "k"),
                ("l", "m"),
                ("m", "l"),
                ("k", "m"),
                ("m", "k"),
                ("e", "b", {"s": "u"})
            ]
            self.g.add_nodes_from(nodes)
            self.g.add_edges_from(edges)
        except:
            self.g = None
            warnings.warn("Could not connect to Neo4j: skipping tests...")

    # @classmethod
    # def teardown_class(self):
    #     self.h._clear()
    #     self.h.close()

    def test_add_node(self):
        # Case 1 : "x" is not in the graph
        if self.g:
            node = "x"
            attrs = {"act": {1}}
            self.g.add_node(node, attrs)
            attrs_node = self.g.get_node(node)
            assert(attrs_node is not None)
            # if (res_node is not None):
            for k in attrs.keys():
                for v in attrs[k]:
                    assert(v in attrs_node[k])

    def test_add_edge(self):
        # Case 1 : (s, t) is not in the graph
        if self.g:
            s = "a"
            t = "d"
            attrs = {"act": {1}}
            self.g.add_edge(s, t, attrs)
            attrs_edge = self.g.get_edge(s, t)
            assert(attrs_edge is not None)
            for k in attrs.keys():
                for v in attrs[k]:
                    assert(v in attrs_edge[k])

    def test_remove_node(self):
        # Case 1 : "a" is in the graph
        if self.g:
            node = "x"
            self.g.remove_node(node)
            attrs = self.g.get_node(node)
            assert(attrs is None or len(attrs) == 0)

    def test_remove_edge(self):
        if self.g:
            s = "a"
            t = "d"
            self.g.remove_edge(s, t)
            attrs_edge = self.g.get_edge(s, t)
            assert(attrs_edge is None or len(attrs_edge) == 0)

    def test_clone_node(self):
        if self.g:
            node = "b"
            clone = "b_clone"
            self.g.clone_node(node, clone)

            # Assert that the 2 nodes have the same properties
            attrs_node = self.g.get_node(node)
            attrs_clone = self.g.get_node(clone)

            assert(set(attrs_node.keys()) == set(attrs_clone.keys()))
            for k in attrs_node.keys():
                if (k != 'id') and (k != 'count'):
                    for v in attrs_node[k]:
                        assert(v in attrs_clone[k])
                elif (k == 'id'):
                    assert(attrs_node[k] != attrs_clone[k])
            # Assert that the 2 nodes have the same successors
            succ_node = self.g.successors(node)
            succ_clone = self.g.successors(clone)
            assert(succ_node == succ_clone)
            # Assert that the 2 nodes have the same predecessors
            pred_node = self.g.predecessors(node)
            pred_clone = self.g.predecessors(clone)
            assert(pred_node == pred_clone)
            # Assert that the edges properties are correctly cloned

    def test_merge_nodes(self):
        # Old node and edges 1
        if self.g:
            n1 = "e"
            attrs_n1 = self.g.get_node(n1)
            succ_n1 = self.g.successors(n1)
            attrs_edge_out_n1 = {}
            for neighbor in succ_n1:
                attrs_edge_out_n1[neighbor] = self.g.get_edge(n1, neighbor)
            pred_n1 = self.g.predecessors(n1)
            attrs_edge_in_n1 = {}
            for neighbor in pred_n1:
                attrs_edge_in_n1[neighbor] = self.g.get_edge(neighbor, n1)

            # Old node and edges 2
            n2 = "g"
            attrs_n2 = self.g.get_node(n2)
            succ_n2 = self.g.successors(n2)
            attrs_edge_out_n2 = {}
            for neighbor in succ_n2:
                attrs_edge_out_n2[neighbor] = self.g.get_edge(n2, neighbor)
            pred_n2 = self.g.predecessors(n2)
            attrs_edge_in_n2 = {}
            for neighbor in pred_n2:
                attrs_edge_in_n2[neighbor] = self.g.get_edge(neighbor, n2)

            # New node and edges
            merged_node = "e_g"
            res = self.g.merge_nodes([n1, n2], merged_node)
            print('-----')
            print(res)
            print('-----')
            attrs_merged = self.g.get_node(merged_node)
            succ_merged = self.g.successors(merged_node)
            attrs_edge_out_merged = {}
            for neighbor in succ_merged:
                attrs_edge_out_merged[neighbor] = self.g.get_edge(merged_node,
                                                                  neighbor)
            pred_merged = self.g.predecessors(merged_node)
            attrs_edge_in_merged = {}
            for neighbor in pred_merged:
                attrs_edge_in_merged[neighbor] = self.g.get_edge(neighbor,
                                                                 merged_node)

            # Assert that the properties are correctly merged
            assert(set(attrs_merged.keys()) ==
                   set(attrs_n1.keys()).union(set(attrs_n2.keys())))
            for k in attrs_n1.keys():
                if (k != 'id') and (k != 'count'):
                    for v in attrs_n1[k]:
                        assert(v in attrs_merged[k])
            for k in attrs_n2.keys():
                if (k != 'id') and (k != 'count'):
                    for v in attrs_n2[k]:
                        assert(v in attrs_merged[k])

            # Assert that the predecesors are conserved
            for pred1 in pred_n1:
                if (pred1 != n1) and (pred1 != n2):
                    assert(pred1 in pred_merged)
                else:
                    assert(merged_node in pred_merged)
            for pred2 in pred_n2:
                if (pred2 != n1) and (pred2 != n2):
                    assert(pred2 in pred_merged)
                else:
                    assert(merged_node in pred_merged)

            # Assert that the successors are conserved
            for suc1 in succ_n1:
                if (suc1 != n1) and (suc1 != n2):
                    assert(suc1 in succ_merged)
                else:
                    assert(merged_node in succ_merged)
            for suc2 in succ_n2:
                if (suc2 != n1) and (suc2 != n2):
                    assert(suc2 in succ_merged)
                else:
                    assert(merged_node in succ_merged)

            # Assert that the in_edges properties are merged correctly
            for k in attrs_edge_in_n1.keys():
                if (k != n1) and (k != n2):
                    for kk in attrs_edge_in_n1[k].keys():
                        for v in attrs_edge_in_n1[k][kk]:
                            assert(v in attrs_edge_in_merged[k][kk])
                else:
                    for kk in attrs_edge_in_n1[k].keys():
                        for v in attrs_edge_in_n1[k][kk]:
                            assert(v in attrs_edge_in_merged[merged_node][kk])
            for k in attrs_edge_in_n2.keys():
                if (k != n1) and (k != n2):
                    for kk in attrs_edge_in_n2[k].keys():
                        for v in attrs_edge_in_n2[k][kk]:
                            assert(v in attrs_edge_in_merged[k][kk])
                else:
                    for kk in attrs_edge_in_n2[k].keys():
                        for v in attrs_edge_in_n2[k][kk]:
                            assert(v in attrs_edge_in_merged[merged_node][kk])

            # Assert that the out_edges properties are merged correctly
            for k in attrs_edge_out_n1.keys():
                if (k != n1) and (k != n2):
                    for kk in attrs_edge_out_n1[k].keys():
                        for v in attrs_edge_out_n1[k][kk]:
                            assert(v in attrs_edge_out_merged[k][kk])
                else:
                    for kk in attrs_edge_in_n1[k].keys():
                        for v in attrs_edge_out_n1[k][kk]:
                            assert(v in attrs_edge_out_merged[merged_node][kk])
            for k in attrs_edge_in_n2.keys():
                if (k != n1) and (k != n2):
                    for kk in attrs_edge_out_n2[k].keys():
                        for v in attrs_edge_out_n2[k][kk]:
                            assert(v in attrs_edge_out_merged[k][kk])
                else:
                    for kk in attrs_edge_out_n2[k].keys():
                        for v in attrs_edge_out_n2[k][kk]:
                            assert(v in attrs_edge_out_merged[merged_node][kk])
