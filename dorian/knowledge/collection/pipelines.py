# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


# useful for handling different item types with a single interface
from dorian.knowledge.collection.items import LibraryItem, FunctionArgs, Functions
from itemadapter import ItemAdapter
import json
import os


class Pipeline:
    def process_item(self, item, spider):
        return item
    
# class LibraryParamsEncoder(json.JSONEncoder):
#     def default(self, obj):
#         if isinstance(obj, LibraryParams):
#             return obj.__dict__
#         return json.JSONEncoder.default(self, obj)

class ScrapyItemEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, 'items'):
            # If the object has 'items' method (like a Scrapy item), convert it to a dictionary.
            return dict(obj)

        elif isinstance(obj, (list, tuple)):
            # Serialize lists or tuples directly.
            return list(obj)

        return json.JSONEncoder.default(self, obj)
    
class SeparateJsonExportPipeline:
    def process_item(self, item, spider):
        # Extract a unique identifier from the item (e.g., 'id' field).
        identifier = item.get('name')
        version = item.get('version')
        # Make sure the identifier is valid before proceeding.
        if identifier:
            # Define the output directory where the JSON files will be saved.
            output_dir = f'dorian/knowledge/collection/data/{spider.language}/{spider.name.split("_")[0]}/'
            if version is not None:
                output_dir=output_dir+"/"+version

            # Create the output directory if it doesn't exist.
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)

            # Generate the filename for the JSON file.
            filename = os.path.join(output_dir, f'{identifier}.json')

            # Save the item to the JSON file.
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(dict(item), f, ensure_ascii=False, cls=ScrapyItemEncoder,indent=4)

            # with open(filename, 'w', encoding='utf-8') as f:
            #     json.dump(dict(item), f, ensure_ascii=False,indent=4)

        return item
