"""
API engine that accesses image/object metadata via a REST API for cases
where we do not have connectivity to the engine via SQL.
"""
from collections import defaultdict

import requests

from splitgraph import switch_engine
from splitgraph.core.image import IMAGE_COLS
from splitgraph.hooks.external_objects import get_external_object_handler


def _image_to_dict(image):
    meta = {i: image.getattr(i) for i in IMAGE_COLS}
    tables = {}
    for table_name in image.get_tables():
        table = image.get_table(table_name)
        tables[table_name] = {'objects': [o[0] for o in table.objects],
                              'schema': table.table_schema}
    meta['tables'] = tables
    return meta


def _object_meta_to_dict(object_meta):
    by_oid = defaultdict(list)
    # TODO external objects
    for object_id, format, parent_id, namespace, size in object_meta:
        by_oid[object_id].append((format, parent_id, namespace, size))

    meta_dict = {k: {'format': vs[0][0],
                     'namespace': vs[0][2],
                     'size': vs[0][3],
                     'parents': [v[1] for v in vs]}
                 for k, vs in by_oid.items()}
    return meta_dict


class APIEngine:
    def __init__(self, hostname, endpoint):
        self.hostname = hostname
        self.endpoint = endpoint

    def _endpoint(self):
        return "http://%s%s" % (self.hostname, self.endpoint)

    def _repo_endpoint(self, repository):
        return "%s/%s/%s" % (self._endpoint(), repository.namespace, repository.repository)

    def get_images(self, repository):
        result = requests.get(self._repo_endpoint(repository) + "/images")
        result.raise_for_status()
        return result.json()

    def put_image(self, image):
        result = requests.put(self._repo_endpoint(image.repository) + "/image/%s" % image.image_hash,
                              data=_image_to_dict(image))
        result.raise_for_status()

    def expand_object_tree(self, object_id):
        result = requests.get(self._endpoint() + "/objects/expand/%s" % object_id)
        result.raise_for_status()
        return list(result.json())

    def get_objects(self, object_ids):
        result = requests.get(self.endpoint() + "/objects", data=object_ids)
        result.raise_for_status()
        return result.json()

    def put_objects(self, object_meta):
        meta_dict = _object_meta_to_dict(object_meta)
        result = requests.put(self._endpoint() + "/objects", data=meta_dict)
        result.raise_for_status()
        # if objects already exist, what do we do


def push(repository, remote_repository, api_engine, handler, handler_params):
    remote_images = api_engine.get_images(remote_repository)
    local_images = list(repository.images)
    new_images = [repository.images.by_hash(ih) for ih in local_images if ih not in remote_images]

    # Get required objects
    required_objects = set()
    for image in new_images:
        for table_name in image.get_tables():
            for object_id, _ in image.get_table(table_name).objects:
                required_objects.add(object_id)

    # Expand the required objects into a full set
    all_required_objects = set()
    for object_id in required_objects:
        all_required_objects.update(repository.objects.get_required_objects(object_id))

    # Check which ones don't exist remotely
    remote_objects = api_engine.get_objects(all_required_objects)
    to_upload = [r for r in all_required_objects if r not in remote_objects]

    # Perform the upload
    external_handler = get_external_object_handler(handler, handler_params)
    with switch_engine(repository.engine):
        uploaded = external_handler.upload_objects(to_upload)
    external_locations = [(oid, url, handler) for oid, url in zip(to_upload, uploaded)]

    # Register the external locations locally
    repository.objects.register_object_locations(external_locations)

    # TODO external locations
    api_engine.put_objects(repository.objects.get_object_meta(to_upload))
    for image in new_images:
        api_engine.put_image(image)

    # TODO tags


def pull(repository, remote_repository, api_engine):
    remote_images = api_engine.get_images(remote_repository)
    local_images = list(repository.images)
    new_images = [im for im in remote_images if im['image_hash'] not in local_images]

    # Register new images and tables; get required objects to materialize those tables.
    required_objects = set()
    new_table_meta = []
    for im in new_images:
        repository.images.add(**{i: im[i] for i in IMAGE_COLS})
        for table_name, table_meta in im['tables']:
            for object_id in table_meta['objects']:
                new_table_meta.append((im['image_hash'], table_name, object_id))
                required_objects.add(object_id)
    repository.objects.register_tables(repository, new_table_meta)

    # Expand the required objects into a full set
    all_required_objects = set()
    for object_id in required_objects:
        all_required_objects.update(api_engine.expand_object_tree(object_id))

    # Check which objects we don't have
    local_objects = repository.objects.get_existing_objects()
    to_download = [r for r in all_required_objects if r not in local_objects]

    # Get the remote metadata
    # TODO demux external locations and put into the table
    remote_meta = api_engine.get_objects(to_download)
    repository.objects.register_objects(remote_meta)

    # TODO tags
