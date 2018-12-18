import logging

import click

import splitgraph as sg
from splitgraph import SplitGraphException
from splitgraph._data.images import _get_all_child_images, delete_images, _get_all_parent_images
from splitgraph.commandline._common import image_spec_parser
from splitgraph.commands.misc import init_driver
from splitgraph.config.keys import KEYS, SENSITIVE_KEYS


@click.command(name='rm')
@click.argument('image_spec', type=image_spec_parser(default=None))
@click.option('-r', '--remote', help="Perform the deletion on a remote instead, specified by its alias")
@click.option('-y', '--yes', help="Agree to deletion without confirmation", is_flag=True, default=False)
def rm_c(image_spec, remote, yes):
    """
    Delete schemas, repositories or images.

    If the target of this command is a Postgres schema, this performs DROP SCHEMA CASCADE.

    If the target of this command is a Splitgraph repository, this deletes the repository and all of its history.

    If the target of this command is an image, this deletes the image and all of its children.

    In any case, this command will ask for confirmation of the deletion, unless ``-y`` is passed. If ``-r``
    (``--remote``) is passed, this will perform deletion on a remote Splitgraph driver (registered in the config)
    instead, assuming the user has write access to the remote repository.

    This does not delete any physical objects that the deleted repository/images depend on:
    use ``sgr cleanup`` to do that.

    Examples:

    ``sgr rm temporary_schema``

        Deletes ``temporary_schema`` from the local driver.

    ``sgr rm --driver splitgraph.com username/repo``

        Deletes ``username/repo`` from the Splitgraph registry.

    ``sgr rm -y username/repo:old_branch``

        Deletes the image pointed to by ``old_branch`` as well as all of its children (images created by a commit based
        on this image), as well as all of the tags that point to now deleted images, without asking for confirmation.
        Note this will not delete images that import tables from the deleted images via Splitfiles or indeed the
        physical objects containing the actual tables.
    """

    repository, image = image_spec
    with sg.do_in_driver(remote):
        if not image:
            print(("Repository" if sg.repository_exists(repository) else "Postgres schema")
                  + " %s will be deleted." % repository.to_schema())
            if not yes:
                click.confirm("Continue? ", abort=True)
            sg.rm(repository)
        else:
            image = sg.resolve_image(repository, image)
            images_to_delete = _get_all_child_images(repository, image)
            tags_to_delete = [t for i, t in sg.get_all_hashes_tags(repository) if i in images_to_delete]

            print("Images to be deleted:")
            print("\n".join(sorted(images_to_delete)))
            print("Total: %d" % len(images_to_delete))

            print("\nTags to be deleted:")
            print("\n".join(sorted(tags_to_delete)))
            print("Total: %d" % len(tags_to_delete))

            if 'HEAD' in tags_to_delete:
                # If we're deleting an image that we currently have checked out,
                # we need to make sure the rest of the metadata (e.g. current state of the audit table) is consistent,
                # it's better to disallow these deletions completely.
                raise SplitGraphException("Deletion will affect a checked-out image! Check out a different branch "
                                          "or do sgr checkout -u %s!" % repository.to_schema())
            if not yes:
                click.confirm("Continue? ", abort=True)

            delete_images(repository, images_to_delete)
            print("Success.")


@click.command(name='prune')
@click.argument('repository', type=sg.to_repository)
@click.option('-r', '--remote', help="Perform the deletion on a remote instead, specified by its alias")
@click.option('-y', '--yes', help="Agree to deletion without confirmation", is_flag=True, default=False)
def prune_c(repository, remote, yes):
    """
    Cleanup dangling images from a repository.

    This includes images not pointed to by any tags (or checked out) and those that aren't required by any of
    such images.

    Will ask for confirmation of the deletion, unless ``-y`` is passed. If ``-r`` (``--remote``) is
    passed, this will perform deletion on a remote Splitgraph driver (registered in the config) instead, assuming
    the user has write access to the remote repository.

    This does not delete any physical objects that the deleted repository/images depend on:
    use ``sgr cleanup`` to do that.
    """
    with sg.do_in_driver(remote):
        all_images = {i[0] for i in sg.get_all_image_info(repository)}
        logging.info(all_images)
        all_tagged_images = {i for i, t in sg.get_all_hashes_tags(repository)}
        logging.info(all_tagged_images)
        dangling_images = all_images.difference(_get_all_parent_images(repository, all_tagged_images))

        if not dangling_images:
            print("Nothing to do.")
            return

        print("Images to be deleted:")
        print("\n".join(sorted(dangling_images)))
        print("Total: %d" % len(dangling_images))

        if not yes:
            click.confirm("Continue? ", abort=True)

        delete_images(repository, dangling_images)
        print("Success.")


@click.command(name='init')
@click.argument('repository', type=sg.to_repository, required=False, default=None)
def init_c(repository):
    """
    Initialize a new repository/driver.

    Examples:

    ``sgr init``

        Initializes the current local Splitgraph driver by writing some bookkeeping information.
        This is required for the rest of sgr to work.

    ``sgr init new/repo``

        Creates a single image with the hash ``00000...`` in ``new/repo``
    """
    if repository:
        sg.init(repository)
        print("Initialized empty repository %s" % str(repository))
    else:
        init_driver()


@click.command(name='cleanup')
def cleanup_c():
    """
    Prune unneeded objects from the driver.

    This deletes all objects from the cache that aren't required by any local repository.
    """
    deleted = sg.cleanup_objects()
    print("Deleted %d physical object(s)" % len(deleted))


@click.command(name='config')
@click.option('-s', '--no-shielding', is_flag=True, default=False,
              help='If set, doesn''t replace sensitive values (like passwords) with asterisks')
@click.option('-c', '--config-format', is_flag=True, default=False,
              help='Output configuration in the Splitgraph config file format')
def config_c(no_shielding, config_format):
    """
    Print the current Splitgraph configuration.

    This takes into account the local config file, the default values
    and all overrides specified via environment variables.

    This command can be used to dump the current Splitgraph configuration into a file::

        sgr config --no-shielding --config-format > .sgconfig

    ...or save a config file overriding an entry::

        SG_REPO_LOOKUP=driver1,driver2 sgr config -sc > .sgconfig
    """

    def _kv_to_str(key, value):
        if not value:
            value_str = ''
        elif key in SENSITIVE_KEYS and not no_shielding:
            value_str = value[0] + '*******'
        else:
            value_str = value
        return "%s=%s" % (key, value_str)

    print("[defaults]\n" if config_format else "", end="")
    # Print normal config parameters
    for key in KEYS:
        print(_kv_to_str(key, sg.CONFIG[key]))

    # Print hoisted remotes
    print("\nCurrent registered remote drivers:\n" if not config_format else "", end="")
    for remote in sg.CONFIG.get('remotes', []):
        print(("\n[remote: %s]" if config_format else "\n%s:") % remote)
        for key, value in sg.CONFIG['remotes'][remote].items():
            print(_kv_to_str(key, value))

    # Print Splitfile commands
    if 'commands' in sg.CONFIG:
        print("\nSplitfile command plugins:\n" if not config_format else "[commands]", end="")
        for command_name, command_class in sg.CONFIG['commands'].items():
            print(_kv_to_str(command_name, command_class))

    # Print mount handlers
    if 'mount_handlers' in sg.CONFIG:
        print("\nFDW Mount handlers:\n" if not config_format else "[mount_handlers]", end="")
        for handler_name, handler_func in sg.CONFIG['mount_handlers'].items():
            print(_kv_to_str(handler_name, handler_func.lower()))

    # Print external object handlers
    if 'external_handlers' in sg.CONFIG:
        print("\nExternal object handlers:\n" if not config_format else "[external_handlers]", end="")
        for handler_name, handler_func in sg.CONFIG['external_handlers'].items():
            print(_kv_to_str(handler_name, handler_func))