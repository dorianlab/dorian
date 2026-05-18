class BKTree:
    def __init__(self, distance_func):
        self._root = None
        self._distance_func = distance_func  # Function to calculate distance between two strings
    
    def add(self, word):
        """Add a word to the BK-tree."""
        if self._root is None:
            self._root = BKNode(word)
        else:
            self._root.add(word, self._distance_func)
    
    def query(self, word, max_distance):
        """Query the tree for words within max_distance of the given word."""
        matches = []
        if self._root is not None:
            self._root.query(word, max_distance, self._distance_func, matches)
        return matches


class BKNode:
    def __init__(self, word):
        self.word = word
        self._children = {}  # Keys are distances, values are child nodes
    
    def add(self, word, distance_func):
        """Add a word to the subtree rooted at this node."""
        distance = distance_func(self.word, word)
        if distance in self._children:
            self._children[distance].add(word, distance_func)
        else:
            self._children[distance] = BKNode(word)
    
    def query(self, word, max_distance, distance_func, matches):
        """Query the subtree rooted at this node."""
        distance = distance_func(self.word, word)
        
        if distance <= max_distance:
            matches.append((self.word, distance))
        
        # Search children where distance is between d-n and d+n
        for d in range(distance - max_distance, distance + max_distance + 1):
            if d in self._children:
                self._children[d].query(word, max_distance, distance_func, matches)
