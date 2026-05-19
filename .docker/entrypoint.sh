#!/bin/bash
set -e

CONFIG=/var/www/html/config.php

if [ ! -f "$CONFIG" ]; then
    cat > "$CONFIG" <<PHP
<?php
unset(\$CFG);
global \$CFG;
\$CFG = new stdClass();

\$CFG->dbtype    = 'sqlsrv';
\$CFG->dblibrary = 'native';
\$CFG->dbhost    = '${MOODLE_DB_HOST}';
\$CFG->dbname    = '${MOODLE_DB_NAME}';
\$CFG->dbuser    = '${MOODLE_DB_USER}';
\$CFG->dbpass    = '${MOODLE_DB_PASS}';
\$CFG->prefix    = 'mdl_';
\$CFG->dboptions = [
    'dbpersist' => false,
    'dbport'    => '${MOODLE_DB_PORT:-1433}',
];

\$CFG->wwwroot  = '${MOODLE_WWWROOT}';
\$CFG->dataroot = '/var/moodledata';
\$CFG->admin    = 'admin';

\$CFG->directorypermissions = 0777;

require_once(__DIR__ . '/lib/setup.php');
PHP
    chown www-data:www-data "$CONFIG"
    chmod 640 "$CONFIG"
    echo "[entrypoint] config.php gerado com sucesso."
fi

mkdir -p /var/moodledata
chown -R www-data:www-data /var/moodledata

exec "$@"
