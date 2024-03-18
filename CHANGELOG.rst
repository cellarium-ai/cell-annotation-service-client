Changelog
#########

All notable changes to Cellarium CAS client will be documented in this file.

The format is based on `Keep a Changelog <https://keepachangelog.com/en/1.0.0/>`_,
and this project adheres to `Semantic Versioning <https://semver.org/spec/v2.0.0.html>`_.

1.4.3 - 2024-03-18
------------------

Added
~~~~~
- Fix total mrna umis for normalized data

Changed
~~~~~~~
- Handle different matrix types in the data preparation callbacks
- Update unit tests for the data preparation callbacks

1.4.2 - 2024-03-12
------------------

Changed
~~~~~~~
- Increase client HTTP request timeouts

1.4.1 - 2024-02-15
------------------

Added
~~~~~
- Include kNN search method (#49)
- Include get cells by IDs method (#49)
- Include helper methods for visualization and demo
- Add model name validation method to :class:`clients.CASClient`
- Add sync POST method (using requests) to :class:`services.CASAPIService`
- Add `CHANGELOG.rst` file
- Add settings module that chooses the correct settings file based on the environment according to current git version. Since now package will use development settings if it's tagged as a pre-release (alpha, beta, or release candidate (rc)), and production settings otherwise.
- Add version determination based on git tags
- Add callback methods to data_preparation module. Include total total_mrna_umis calculation as a callback before data sanitization

Changed
~~~~~~~
- Reorganize :class:`CASClient` methods: factor out sharding logic
- Update `MAX_NUM_REQUESTS_AT_A_TIME` to 25
- Update default `chunk_size` in :meth:`annotate` methods to 1000
- Make :meth:`__validate_and_sanitize_input_data` method public (now it's a :meth:`validate_and_sanitize_input_data`) in CASClient
- Update backend API url to point to the new API endpoints depending on the environment
- Update `pyproject.toml` file to include scanpy optional dependencies
- Restructure data_preparation into a module.

Removed
~~~~~~~
- Remove docs generation from CI/CD pipeline

File Structure Changes
~~~~~~~~~~~~~~~~~~~~~~
- Add `CHANGELOG.rst` file
- Add `requirements/scanpy.txt` file (optional requirements for scanpy related demos)
- Add `cellarium/cas/scanpy_utils.py` (Not necessary for the client methods, but useful for the demo)
- Add `cellarium/cas/settings` directory, including `__init__.py`, `base.py`, `development.py`, and `production.py` files
- Add cas/version.py file
- Add `cellarium/cas/data_preparation` directory, including `__init__.py`, `callbacks.py`, `sanitizer.py` and `validator.py` files
- Add `tests/unit/test_data_preparation_callbacks.py` file
- Add `cellarium/cas/constants.py` file
- Remove `.github/actions/docs` folder (docs are now hosted on readthedocs)

Notes
~~~~~
- Users will need a new API token to use this version
