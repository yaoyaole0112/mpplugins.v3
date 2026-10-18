from p115client.tool.download import iter_download_nodes
from p115client.tool.iterdir import ensure_path


def iter_files_skim_serial(
    client,
    cid=0,
    *,
    with_path=False,
    with_ancestors=False,
    app="web",
    id_to_dirnode=None,
    **request_kwargs,
):
    if id_to_dirnode is None:
        id_to_dirnode = {}
    attrs = iter_download_nodes(
        client,
        cid,
        page_size=500,
        ensure_name=True,
        max_workers=0,
        cooldown=1.5,
        id_to_dirnode=id_to_dirnode,
        app=app,
        **request_kwargs,
    )
    if with_path or with_ancestors:
        yield from ensure_path(
            client,
            cid,
            attrs,
            with_path=with_path,
            with_ancestors=with_ancestors,
            path_already=True,
            id_to_dirnode=id_to_dirnode,
            app=app,
            **request_kwargs,
        )
    else:
        yield from attrs
