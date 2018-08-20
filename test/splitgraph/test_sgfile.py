import os
import tempfile

import pytest

from splitgraph.commands import checkout, commit, push, unmount, clone, get_log
from splitgraph.commands.misc import cleanup_objects
from splitgraph.constants import SPLITGRAPH_META_SCHEMA, PG_USER, PG_PWD, PG_DB, SplitGraphException
from splitgraph.meta_handler import get_current_head, get_snap_parent, set_tag, get_current_mountpoints_hashes, \
    get_downloaded_objects, get_existing_objects, get_external_object_locations
from splitgraph.pg_utils import pg_table_exists
from splitgraph.sgfile import execute_commands, preprocess
from test.splitgraph.conftest import SNAPPER_HOST, SNAPPER_PORT

SGFILE_ROOT = os.path.join(os.path.dirname(__file__), '../resources/')


def _load_sgfile(name):
    with open(SGFILE_ROOT + name, 'r') as f:
        return f.read()


PARSING_TEST_SGFILE = _load_sgfile('import_remote_multiple.sgfile')


def test_sgfile_preprocessor_missing_params():
    with pytest.raises(SplitGraphException) as e:
        preprocess(PARSING_TEST_SGFILE, params={'SNAPPER': '127.0.0.1'})
    assert '${TAG}' in str(e.value)
    assert '${SNAPPER_PORT}' in str(e.value)
    assert '${SNAPPER}' not in str(e.value)
    assert '${ESCAPED}' not in str(e.value)


def test_sgfile_preprocessor_escaping():
    commands = preprocess(PARSING_TEST_SGFILE, params={'SNAPPER': '127.0.0.1', 'SNAPPER_PORT': 5678, 'TAG': 'tag-v1-whatever'})
    print(commands)
    assert '${TAG}' not in commands
    assert '${SNAPPER}' not in commands
    assert '\\${ESCAPED}' not in commands
    assert '${ESCAPED}' in commands
    assert '127.0.0.1' in commands
    assert '5678' in commands
    assert 'tag-v1-whatever' in commands


def test_basic_sgfile(sg_pg_mg_conn):
    execute_commands(sg_pg_mg_conn, _load_sgfile('create_table.sgfile'))
    log = list(reversed(get_log(sg_pg_mg_conn, 'output', get_current_head(sg_pg_mg_conn, 'output'))))

    checkout(sg_pg_mg_conn, 'output', log[1])
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT * FROM output.my_fruits""")
        assert cur.fetchall() == []

    checkout(sg_pg_mg_conn, 'output', log[2])
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT * FROM output.my_fruits""")
        assert cur.fetchall() == [(1, 'pineapple')]

    checkout(sg_pg_mg_conn, 'output', log[3])
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT * FROM output.my_fruits""")
        assert cur.fetchall() == [(1, 'pineapple'), (2, 'banana')]


def test_update_without_import_sgfile(sg_pg_mg_conn):
    # Test that correct commits are produced by executing an sgfile (both against newly created and already
    # existing tables on an existing mountpoint)
    execute_commands(sg_pg_mg_conn, _load_sgfile('update_without_import.sgfile'))
    log = get_log(sg_pg_mg_conn, 'test_pg_mount', get_current_head(sg_pg_mg_conn, 'test_pg_mount'))

    # insert into my_fruits
    checkout(sg_pg_mg_conn, 'test_pg_mount', log[1])
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT * FROM test_pg_mount.my_fruits""")
        assert cur.fetchall() == [(1, 'pineapple')]
        cur.execute("""SELECT * FROM test_pg_mount.fruits""")
        assert cur.fetchall() == [(1, 'apple'), (2, 'orange')]

    # insert into fruits
    checkout(sg_pg_mg_conn, 'test_pg_mount', log[0])
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT * FROM test_pg_mount.my_fruits""")
        assert cur.fetchall() == [(1, 'pineapple')]
        cur.execute("""SELECT * FROM test_pg_mount.fruits""")
        assert cur.fetchall() == [(1, 'apple'), (2, 'orange'), (3, 'mayonnaise')]


def test_local_import_sgfile(sg_pg_mg_conn):
    execute_commands(sg_pg_mg_conn, _load_sgfile('import_local.sgfile'))
    head = get_current_head(sg_pg_mg_conn, 'output')
    old_head = get_snap_parent(sg_pg_mg_conn, 'output', head)

    checkout(sg_pg_mg_conn, 'output', old_head)
    assert not pg_table_exists(sg_pg_mg_conn, 'output', 'my_fruits')
    assert not pg_table_exists(sg_pg_mg_conn, 'output', 'fruits')

    checkout(sg_pg_mg_conn, 'output', head)
    assert pg_table_exists(sg_pg_mg_conn, 'output', 'my_fruits')
    assert not pg_table_exists(sg_pg_mg_conn, 'output', 'fruits')


def test_advanced_sgfile(sg_pg_mg_conn):
    execute_commands(sg_pg_mg_conn, _load_sgfile('import_local_multiple_with_queries.sgfile'))
    head = get_current_head(sg_pg_mg_conn, 'output')

    assert pg_table_exists(sg_pg_mg_conn, 'output', 'my_fruits')
    assert pg_table_exists(sg_pg_mg_conn, 'output', 'vegetables')
    assert not pg_table_exists(sg_pg_mg_conn, 'output', 'fruits')
    assert pg_table_exists(sg_pg_mg_conn, 'output', 'join_table')

    old_head = get_snap_parent(sg_pg_mg_conn, 'output', head)
    checkout(sg_pg_mg_conn, 'output', old_head)
    assert not pg_table_exists(sg_pg_mg_conn, 'output', 'join_table')
    checkout(sg_pg_mg_conn, 'output', head)
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT id, fruit, vegetable FROM output.join_table""")
        assert cur.fetchall() == [(2, 'orange', 'carrot')]
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT * FROM output.my_fruits""")
        assert cur.fetchall() == [(2, 'orange')]


def test_sgfile_cached(sg_pg_mg_conn):
    # Check that no new commits/snaps are created if we rerun the same sgfile
    execute_commands(sg_pg_mg_conn, _load_sgfile('import_local_multiple_with_queries.sgfile'))
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT snap_id FROM %s.snap_tree WHERE mountpoint = 'output'""" % SPLITGRAPH_META_SCHEMA)
        commits = [c[0] for c in cur.fetchall()]
    assert len(commits) == 4

    execute_commands(sg_pg_mg_conn, _load_sgfile('import_local_multiple_with_queries.sgfile'))
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT snap_id FROM %s.snap_tree WHERE mountpoint = 'output'""" % SPLITGRAPH_META_SCHEMA)
        new_commits = [c[0] for c in cur.fetchall()]
    assert new_commits == commits


def test_sgfile_remote(sg_pg_mg_conn, snapper_conn):
    # Give test_pg_mount on the remote tag v1
    set_tag(snapper_conn, 'test_pg_mount', get_current_head(snapper_conn, 'test_pg_mount'), 'v1')
    with snapper_conn.cursor() as cur:
        cur.execute("DELETE FROM test_pg_mount.fruits WHERE fruit_id = 1")
    new_head = commit(snapper_conn, 'test_pg_mount')
    set_tag(snapper_conn, 'test_pg_mount', new_head, 'v2')
    snapper_conn.commit()

    # We use the v1 tag when importing from the remote, so fruit_id = 1 still exists there.
    execute_commands(sg_pg_mg_conn, _load_sgfile('import_remote_multiple.sgfile'), params={'SNAPPER': SNAPPER_HOST,
                                                                                           'SNAPPER_PORT': SNAPPER_PORT,
                                                                                           'TAG': 'v1'})
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT id, fruit, vegetable FROM output.join_table""")
        assert cur.fetchall() == [(1, 'apple', 'potato'), (2, 'orange', 'carrot')]

    # Now run the commands against v2 and make sure the fruit_id = 1 has disappeared from the output.
    execute_commands(sg_pg_mg_conn, _load_sgfile('import_remote_multiple.sgfile'), params={'SNAPPER': SNAPPER_HOST,
                                                                                           'SNAPPER_PORT': SNAPPER_PORT,
                                                                                           'TAG': 'v2'})
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT id, fruit, vegetable FROM output.join_table""")
        assert cur.fetchall() == [(2, 'orange', 'carrot')]


def test_import_updating_sgfile_with_uploading(empty_pg_conn, snapper_conn):
    execute_commands(empty_pg_conn, _load_sgfile('import_and_update.sgfile'), params={'SNAPPER': SNAPPER_HOST,
                                                                                      'SNAPPER_PORT': SNAPPER_PORT})
    head = get_current_head(empty_pg_conn, 'output')

    assert len(get_existing_objects(empty_pg_conn)) == 4  # Two original tables + two updates

    with tempfile.TemporaryDirectory() as tmpdir:
        # Push with upload
        conn_string = '%s:%s@%s:%s/%s' % (PG_USER, PG_PWD, SNAPPER_HOST, SNAPPER_PORT, PG_DB)
        push(empty_pg_conn, conn_string, 'output', 'output',
             handler='FILE', handler_options={'path': tmpdir})
        # Unmount everything locally and cleanup
        unmount(empty_pg_conn, 'output')
        cleanup_objects(empty_pg_conn)
        assert not get_existing_objects(empty_pg_conn)

        clone(empty_pg_conn, conn_string, 'output', 'output', download_all=False)

        assert not get_downloaded_objects(empty_pg_conn)
        existing_objects = list(get_existing_objects(empty_pg_conn))
        assert len(existing_objects) == 4  # Two original tables + two updates
        # Only 2 objects are stored externally (the other two have been on the remote the whole time)
        assert len(get_external_object_locations(empty_pg_conn, existing_objects)) == 2

        checkout(empty_pg_conn, 'output', head)
        with empty_pg_conn.cursor() as cur:
            cur.execute("""SELECT fruit_id, name FROM output.my_fruits""")
            assert cur.fetchall() == [(1, 'apple'), (2, 'orange'), (3, 'mayonnaise')]

            cur.execute("""SELECT vegetable_id, name FROM output.vegetables""")
            assert sorted(cur.fetchall()) == [(1, 'cucumber'), (2, 'carrot')]


def test_sgfile_end_to_end_with_uploading(sg_pg_mg_conn, snapper_conn):
    # An end-to-end test:
    #   * Create a derived dataset from some tables imported from the snapper
    #   * Push it back to the snapper, uploading all objects to "HTTP" (instead of the snapper itself)
    #   * Delete everything from pgcache
    #   * Run another sgfile that depends on the just-pushed dataset (and does lazy checkouts to
    #     get the required tables).

    # Do the same setting up first and run the sgfile against the remote data.
    set_tag(snapper_conn, 'test_pg_mount', get_current_head(snapper_conn, 'test_pg_mount'), 'v1')
    with snapper_conn.cursor() as cur:
        cur.execute("DELETE FROM test_pg_mount.fruits WHERE fruit_id = 1")
    new_head = commit(snapper_conn, 'test_pg_mount')
    set_tag(snapper_conn, 'test_pg_mount', new_head, 'v2')
    snapper_conn.commit()
    execute_commands(sg_pg_mg_conn, _load_sgfile('import_remote_multiple.sgfile'), params={'SNAPPER': SNAPPER_HOST,
                                                                                           'TAG': 'v1',
                                                                                           'SNAPPER_PORT': SNAPPER_PORT})

    with tempfile.TemporaryDirectory() as tmpdir:
        # Push with upload
        push(sg_pg_mg_conn, '%s:%s@%s:%s/%s' % (PG_USER, PG_PWD, SNAPPER_HOST, SNAPPER_PORT, PG_DB), 'output', 'output',
             handler='FILE', handler_options={'path': tmpdir})
        # Unmount everything locally and cleanup
        for mountpoint, _ in get_current_mountpoints_hashes(sg_pg_mg_conn):
            unmount(sg_pg_mg_conn, mountpoint)
        cleanup_objects(sg_pg_mg_conn)

        execute_commands(sg_pg_mg_conn, _load_sgfile('import_from_preuploaded_remote.sgfile'), params={'SNAPPER': SNAPPER_HOST,
                                                                                                       'SNAPPER_PORT': SNAPPER_PORT})

        with sg_pg_mg_conn.cursor() as cur:
            cur.execute("""SELECT id, name, fruit, vegetable FROM output_stage_2.diet""")
            assert cur.fetchall() == [(2, 'James', 'orange', 'carrot')]


def test_sgfile_schema_changes(sg_pg_mg_conn):
    execute_commands(sg_pg_mg_conn, _load_sgfile('schema_changes.sgfile'))
    old_output_head = get_current_head(sg_pg_mg_conn, 'output')

    # Then, alter the dataset and rerun the sgfile.
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""INSERT INTO test_pg_mount.fruits VALUES (12, 'mayonnaise')""")
    commit(sg_pg_mg_conn, 'test_pg_mount')
    execute_commands(sg_pg_mg_conn, _load_sgfile('schema_changes.sgfile'))
    new_output_head = get_current_head(sg_pg_mg_conn, 'output')

    checkout(sg_pg_mg_conn, 'output', old_output_head)
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT * FROM output.spirit_fruits""")
        assert cur.fetchall() == [('James', 'orange', 12)]

    checkout(sg_pg_mg_conn, 'output', new_output_head)
    with sg_pg_mg_conn.cursor() as cur:
        cur.execute("""SELECT * FROM output.spirit_fruits""")
        # Mayonnaise joined with Alex, ID 12 + 10 = 22.
        assert cur.fetchall() == [('James', 'orange', 12), ('Alex', 'mayonnaise', 22)]
