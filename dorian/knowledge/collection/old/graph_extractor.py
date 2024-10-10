import os

from evaluation.stat_collector import StatCollector
import hooks
from rule_executioner import transform_graph
from tree_sitter import Language, Parser
import test_scripts
import time
from regraph import NXGraph


class GraphExtractor:

    def __init__(self):
        Language.build_library(
            # Store the library in the `build` directory
            'build/my-languages.so',
            # Include one or more languages
            [
                os.path.normpath(os.path.join(os.path.dirname(__file__), '../third_party/parsers/tree-sitter-python')),
                os.path.normpath(os.path.join(os.path.dirname(__file__), '../third_party/parsers/tree-sitter-r')),
                os.path.normpath(os.path.join(os.path.dirname(__file__), '../third_party/parsers/tree-sitter-snakemake-pure'))

            ]
        )

    def extract_pipeline(self, code, language, hook):
        code_language = language
        start_time = time.time()
        # set language for the parser
        assert language != '', 'language is not set'
        language = Language('build/my-languages.so', language)
        parser = Parser()
        parser.set_language(language)
        b = bytes(code, "utf8")
        ast_start = time.time()
        # let tree-sitter parse code into a tree
        tree = parser.parse(b)
        nxgraph = self.bfs_tree_traverser(tree)
        ast_end = time.time()
        # transform tree into a data science pipeline
        G = transform_graph(nxgraph, code_language, hook)
        end_time = time.time()
        StatCollector.getStatCollector().append_script_data({'code lines': tree.root_node.child_count})
        StatCollector.getStatCollector().append_script_data({'total time': round(end_time - start_time, 2)})
        StatCollector.getStatCollector().append_script_data({'ast time': round(ast_end - ast_start, 2)})
        #print(round(end_time - start_time, 2))
        return G

    def bfs_tree_traverser(self, tree):
        """
        Traverses a tree-sitter with Breadth-first search algorithm and
        converts it into an NXGraph
        :param tree: tree-sitter to be traversed
        :return: NXGraph after traversal of a tree-sitter tree
        """
        root_node = tree.root_node
        G = NXGraph()
        node_id, parent_id = 0, 0
        # lists to queue the nodes in order and identify already visited nodes
        visited, queue = [], []
        visited.append(root_node)
        queue.append(root_node)

        # add root_node to the graph
        G.add_node(0, attrs={"type": root_node.type, "text": root_node.text.decode('utf-8')})

        # loop to visit each node
        while queue:
            node = queue.pop(0)
            for child_node in node.children:
                if child_node not in visited:
                    node_id += 1
                    # add child node to graph
                    G.add_node(node_id, attrs={"type": child_node.type, "text": child_node.text.decode('utf-8')})
                    # add edge between parent_node and child_node
                    G.add_edge(parent_id, node_id)
                    visited.append(child_node)
                    queue.append(child_node)
            # set parent_id to the id of the next node in queue
            parent_id = parent_id + 1
        return G


if __name__ == "__main__":
    start_time = time.time()
    language = 'python'
    code = test_scripts.Python.code_0
    start = time.time()
    extractor = GraphExtractor()
    extractor.extract_pipeline(code, language, hooks.PythonHook.PythonHook())
    print("--- %s seconds ---" % (time.time() - start_time))
