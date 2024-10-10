from regraph import NXGraph

class LanguageHook:
    def pre_hooks(self, G: NXGraph):
        pass

    def post_hooks(self, G: NXGraph):
        pass

def get_hook() -> LanguageHook:
    pass