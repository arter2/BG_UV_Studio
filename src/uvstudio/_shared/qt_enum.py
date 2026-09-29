def _enum(owner, *dotted_names):
    """Resolve the first attribute path that exists.

    PySide6 scopes enums where PySide2 flattened them. Most flat forms still
    resolve under PySide6, but the ones that do not fail at import time.
    """
    for dotted in dotted_names:
        obj = owner
        ok = True
        for part in dotted.split("."):
            if hasattr(obj, part):
                obj = getattr(obj, part)
            else:
                ok = False
                break
        if ok:
            return obj
    raise AttributeError("None of %r resolve under %s"
                         % (dotted_names, QT_BINDING))
