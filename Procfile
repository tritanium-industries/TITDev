web: env PYTHONPATH=$PYTHONPATH:$PWD/views:$PWD/resources:$PWD/helpers newrelic-admin run-program gunicorn -b "0.0.0.0:$PORT" -w 3 main:app --log-file=-