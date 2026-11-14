"""Konfigurace projektu mini-wms.

PyMySQL se registruje jako ovladač ``MySQLdb``, takže Django backend
``django.db.backends.mysql`` funguje s MariaDB bez nutnosti kompilovat mysqlclient.
"""

try:
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:  # pragma: no cover - PyMySQL je v requirements.txt
    pass
