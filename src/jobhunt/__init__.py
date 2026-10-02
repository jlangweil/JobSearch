"""jobhunt: personal job-search system."""

import os

# SQLAlchemy ships optional compiled speed-ups (sqlalchemy/cyextension/*.pyd) that are not code-signed. Windows 11
# Smart App Control blocks them and shows a "Part of this app has been blocked" notification; SQLAlchemy then falls
# back to its pure-Python implementation anyway. Opting out up front avoids the blocked load and the popup. This
# must run before anything imports SQLAlchemy, which is why it lives in the package __init__.
os.environ.setdefault("DISABLE_SQLALCHEMY_CEXT_RUNTIME", "1")
