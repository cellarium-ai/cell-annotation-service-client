import asyncio
import datetime
import functools
import math
import operator
import time
import typing as t
import warnings

import anndata

from cellarium.cas import _io, data_preparation, exceptions, service

NUM_ATTEMPTS_PER_CHUNK_DEFAULT = 7
MAX_NUM_REQUESTS_AT_A_TIME = 60


class CASClient:
    """
    Service that is designed to communicate with the Cellarium Cloud Backend.

    :param api_token: API token issued by the Cellarium team
    :param num_attempts_per_chunk: Number of attempts the client should make to annotate each chunk. |br|
        `Default:` ``3``
    """

    def _print_models(self, models):
        s = "Allowed model list in Cellarium CAS:\n"
        for model in models:
            model_name = model["model_name"]
            model_schema = model["schema_name"]
            embedding_dimension = model["embedding_dimension"]
            if model["is_default_model"]:
                model_name += " (default)"

            s += f"  - {model_name}\n    Schema: {model_schema}\n    Embedding dimension: {embedding_dimension}\n"

        self._print(s)

    def __init__(self, api_token: str, num_attempts_per_chunk: int = NUM_ATTEMPTS_PER_CHUNK_DEFAULT) -> None:
        self.cas_api_service = service.CASAPIService(api_token=api_token)

        self._print("Connecting to the Cellarium Cloud backend...")
        self.cas_api_service.validate_token()

        # Retrieving General Info
        application_info = self.cas_api_service.get_application_info()

        self.model_objects_list = self.cas_api_service.get_model_list()
        self.allowed_models_list = [x["model_name"] for x in self.model_objects_list]
        self._model_name_obj_map = {x["model_name"]: x for x in self.model_objects_list}
        self.default_model_name = [x["model_name"] for x in self.model_objects_list if x["is_default_model"]][0]

        self.feature_schemas = self.cas_api_service.get_feature_schemas()
        self._feature_schemas_cache = {}

        self.num_attempts_per_chunk = num_attempts_per_chunk
        self._print(f"Authenticated in Cellarium Cloud v. {application_info['application_version']}")

        self._print_models(self.model_objects_list)

    @staticmethod
    def _get_number_of_chunks(adata, chunk_size):
        return math.ceil(len(adata) / chunk_size)

    @staticmethod
    def _get_timestamp() -> str:
        return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def _print(self, str_to_print: str) -> None:
        print(f"* [{self._get_timestamp()}] {str_to_print}")

    def _validate_and_sanitize_input_data(
        self,
        adata: anndata.AnnData,
        cas_model_name: str,
        count_matrix_name: str,
        feature_ids_column_name: str,
    ) -> anndata.AnnData:
        cas_model_obj = self._model_name_obj_map[cas_model_name]
        feature_schema_name = cas_model_obj["schema_name"]
        try:
            cas_feature_schema_list = self._feature_schemas_cache[feature_schema_name]
        except KeyError:
            cas_feature_schema_list = self.cas_api_service.get_feature_schema_by(name=feature_schema_name)
            self._feature_schemas_cache[feature_schema_name] = cas_feature_schema_list

        try:
            data_preparation.validate(
                adata=adata,
                cas_feature_schema_list=cas_feature_schema_list,
                feature_ids_column_name=feature_ids_column_name,
            )
        except exceptions.DataValidationError as e:
            if e.extra_features > 0:
                self._print(
                    f"The input data matrix has {e.extra_features} extra features compared to '{feature_schema_name}' "
                    f"CAS schema ({len(cas_feature_schema_list)}). "
                    f"Extra input features will be dropped."
                )
            if e.missing_features > 0:
                self._print(
                    f"The input data matrix has {e.missing_features} missing features compared to "
                    f"'{feature_schema_name}' CAS schema ({len(cas_feature_schema_list)}). "
                    f"Missing features will be imputed with zeros."
                )
            if e.extra_features == 0 and e.missing_features == 0:
                self._print(
                    f"Input datafile has all the necessary features as {feature_schema_name}, but it's still "
                    f"incompatible because of the different order. The features will be reordered "
                    f"according to {feature_schema_name}..."
                )
                self._print(
                    f"The input data matrix contains all of the features specified in '{feature_schema_name}' "
                    f"CAS schema but in a different order. The input features will be reordered according to "
                    f"'{feature_schema_name}'"
                )
            return data_preparation.sanitize(
                adata=adata,
                cas_feature_schema_list=cas_feature_schema_list,
                count_matrix_name=count_matrix_name,
                feature_ids_column_name=feature_ids_column_name,
            )
        else:
            self._print(f"The input data matrix conforms with the '{feature_schema_name}' CAS schema.")
            return adata

    async def _annotate_anndata_chunk_task(
        self,
        adata_bytes: bytes,
        results: t.List,
        chunk_index: int,
        model_name: str,
        chunk_start_i: int,
        chunk_end_i: int,
        semaphore: asyncio.Semaphore,
        include_dev_metadata: bool = False,
    ) -> None:
        """
        A wrapper around POST request that handles HTTP and client errors, and resubmits the task if necessary.

        In case of HTTP 500, 503, 504 or ClientError print a message and resubmit the task.
        In case of HTTP 401 print a message to check the token.
        In case of any other error print a message.

        :param adata_bytes: Dumped bytes of `anndata.AnnData` chunk
        :param results: Results list that needs to be used to inplace the response from the server
        :param model_name: A name of the model to use for annotations
        :param chunk_index: Consequent number of the chunk (e.g. Chunk 1, Chunk 2)
        :param chunk_start_i: Index pointing to the main adata file start position of the current chunk
        :param chunk_end_i: Index pointing to the main adata file end position of the current chunk
        :param semaphore: Semaphore object to limit the number of concurrent requests at a time
        :param include_dev_metadata: Boolean indicating whether to include a breakdown of the number of cells by dataset
        """
        async with semaphore:
            number_of_cells = chunk_end_i - chunk_start_i

            retry_delay = 5
            max_retry_delay = 32

            for _ in range(self.num_attempts_per_chunk):
                try:
                    results[chunk_index] = await self.cas_api_service.async_annotate_anndata_chunk(
                        adata_file_bytes=adata_bytes,
                        number_of_cells=number_of_cells,
                        model_name=model_name,
                        include_dev_metadata=include_dev_metadata,
                    )

                except (exceptions.HTTPError5XX, exceptions.HTTPClientError) as e:
                    self._print(str(e))
                    self._print(
                        f"Resubmitting chunk #{chunk_index + 1:2.0f} ({chunk_start_i:5.0f}, {chunk_end_i:5.0f}) to CAS ..."
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)
                    continue
                except exceptions.HTTPError401:
                    self._print("Unauthorized token. Please check your API token or request a new one.")
                    break
                except Exception as e:
                    self._print(f"Unexpected error: {e.__class__.__name__}; Message: {str(e)}")
                    break
                else:
                    self._print(
                        f"Received the annotations for cell chunk #{chunk_index + 1:2.0f} ({chunk_start_i:5.0f}, "
                        f"{chunk_end_i:5.0f}) ..."
                    )
                    break

    async def _annotate_anndata(
        self,
        adata: "anndata.AnnData",
        model_name: str,
        results: t.List,
        chunk_size: int,
        include_dev_metadata: bool = False,
    ) -> None:
        """
        Submit chunks asynchronously as asyncio tasks

        :param adata: `anndata.AnnData` file to process
        :param model_name: Model name to use for annotations
        :param results: Results list that is used to inplace the responses from the server
        :param chunk_size: Chunk size to split on
        :param include_dev_metadata: Boolean indicating whether to include a breakdown of the number of cells by dataset
        """
        i, j = 0, chunk_size
        minibatch_size = 10
        number_of_chunks = self._get_number_of_chunks(adata, chunk_size=chunk_size)
        for minibatch_start in range(number_of_chunks)[::minibatch_size]:
            tasks = []
            for chunk_index in range(minibatch_start, min(minibatch_start + minibatch_size, number_of_chunks)):
                chunk = adata[i:j, :]
                chunk_start_i = i
                chunk_end_i = i + len(chunk)
                self._print(
                    f"Submitting cell chunk #{chunk_index + 1:2.0f} ({chunk_start_i:5.0f}, {chunk_end_i:5.0f}) to CAS ..."
                )
                tmp_file_name = f"chunk_{chunk_index}.h5ad"
                chunk.write(tmp_file_name, compression="gzip")
                with open(tmp_file_name, "rb") as f:
                    chunk_bytes = f.read()

                os.remove(tmp_file_name)
                tasks.append(
                    self._annotate_anndata_chunk(
                        adata_bytes=chunk_bytes,
                        results=results,
                        model_name=model_name,
                        chunk_index=chunk_index,
                        chunk_start_i=chunk_start_i,
                        chunk_end_i=chunk_end_i,
                    )
                )
                i = j
                j += chunk_size

            await asyncio.wait(tasks)

    def __postprocess_annotations(
        self, annotation_query_response: t.List[t.Dict[str, t.Any]], adata: "anndata.AnnData"
    ) -> t.List[t.Dict[str, t.Any]]:
        """
        Postprocess annotations by matching the order of cells in the response with the order of cells in the input

        :param annotation_query_response: List of dictionaries with annotations for each of the cells from input adata
        :param adata: :class:`anndata.AnnData` instance to annotate

        :return: A list of dictionaries with annotations for each of the cells from input adata, ordered by the input
        """

        processed_response = []
        annotation_query_response_hash = {x["query_cell_id"]: x for x in annotation_query_response}

        num_unannotated_cells = 0

        for query_cell_id in adata.obs.index:
            try:
                query_annotation = annotation_query_response_hash[query_cell_id]
            except KeyError:
                query_annotation = {"query_cell_id": query_cell_id, "matches": []}
                num_unannotated_cells += 1

            processed_response.append(query_annotation)

        if num_unannotated_cells > 0:
            self._print(f"{num_unannotated_cells} cells were not annotated by CAS")

        return processed_response

    def annotate_anndata(
        self,
        adata: "anndata.AnnData",
        chunk_size=2000,
        cas_model_name: str = "default",
        count_matrix_name: str = "X",
        feature_ids_column_name: str = "index",
        include_dev_metadata: bool = False,
    ) -> t.List[t.Dict[str, t.Any]]:
        """
        Send an instance of :class:`anndata.AnnData` to the Cellarium Cloud backend for annotations. The function
        splits the ``adata`` into smaller chunks and asynchronously sends them to the backend API service. Each chunk is
        of equal size, except for the last one, which may be smaller. The backend processes these chunks in parallel.

        :param adata: :class:`anndata.AnnData` instance to annotate
        :param chunk_size: Size of chunks to split on
        :param cas_model_name: Model name to use for annotation. |br|
            `Allowed Values:` Model name from the :attr:`allowed_models_list` list or ``"default"``
            keyword, which refers to the default selected model in the Cellarium backend. |br|
            `Default:` ``"default"``
        :param count_matrix_name:  Where to obtain a feature expression count matrix from. |br|
            `Allowed Values:` Choice of either ``"X"``  or ``"raw.X"`` in order to use ``adata.X`` or ``adata.raw.X``,
             respectively |br|
            `Default:` ``"X"``
        :param feature_ids_column_name: Column name where to obtain Ensembl feature ids. |br|
            `Allowed Values:` A value from ``adata.var.columns`` or ``"index"`` keyword, which refers to index
            column. |br|
            `Default:` ``"index"``
        :param include_dev_metadata: Boolean indicating whether to include a breakdown of the number of cells
            by dataset
        :return: A list of dictionaries with annotations for each of the cells from input adata
        """
        assert cas_model_name == "default" or cas_model_name in self.allowed_models_list, (
            "`cas_model_name` should have a value of either 'default' or one of the values from "
            "`allowed_models_list`."
        )
        cas_model_name = self.default_model_name if cas_model_name == "default" else cas_model_name
        start = time.time()
        cas_model = self._model_name_obj_map[cas_model_name]
        cas_model_name = cas_model["model_name"]
        self._print(f"Cellarium CAS (Model ID: {cas_model_name})")
        self._print(f"Total number of input cells: {len(adata)}")
        adata = self._validate_and_sanitize_input_data(
            adata=adata,
            cas_model_name=cas_model_name,
            count_matrix_name=count_matrix_name,
            feature_ids_column_name=feature_ids_column_name,
        )

        number_of_chunks = math.ceil(len(adata) / chunk_size)
        results = [[] for _ in range(number_of_chunks)]

        asyncio.run(
            self._annotate_anndata(
                adata=adata,
                results=results,
                chunk_size=chunk_size,
                model_name=cas_model_name,
                include_dev_metadata=include_dev_metadata,
            )
        )

        result = functools.reduce(operator.iconcat, results, [])
        result = self.__postprocess_annotations(result, adata)

        self._print(f"Total wall clock time: {f'{time.time() - start:10.4f}'} seconds")

        if len(result) != len(adata):
            raise exceptions.CASClientError(
                "Number of cells in the result doesn't match the number of cells in `adata`"
            )

        self._print("Finished!")
        return result

    def annotate_anndata_file(
        self,
        filepath: str,
        chunk_size=2000,
        cas_model_name: str = "default",
        count_matrix_name: str = "X",
        feature_ids_column_name: str = "index",
        include_dev_metadata: bool = False,
    ) -> t.List[t.Dict[str, t.Any]]:
        """
        Read the 'h5ad' file into a :class:`anndata.AnnData` matrix and apply the :meth:`annotate_anndata` method to it.

        :param filepath: Filepath of the local :class:`anndata.AnnData` matrix
        :param chunk_size: Size of chunks to split on
        :param cas_model_name: Model name to use for annotation. |br|
            `Allowed Values:` Model name from the :attr:`allowed_models_list` list or ``"default"``
            keyword, which refers to the default selected model in the Cellarium backend. |br|
            `Default:` ``"default"``
        :param count_matrix_name:  Where to obtain a feature expression count matrix from. |br|
            `Allowed Values:` Choice of either ``"X"``  or ``"raw.X"`` in order to use ``adata.X`` or ``adata.raw.X``,
             respectively |br|
            `Default:` ``"X"``
        :param feature_ids_column_name: Column name where to obtain Ensembl feature ids. |br|
            `Allowed Values:` A value from ``adata.var.columns`` or ``"index"`` keyword, which refers to index
            column. |br|
            `Default:` ``"index"``
        :param include_dev_metadata: Boolean indicating whether to include a breakdown of the number of cells
            per dataset
        :return: A list of dictionaries with annotations for each of the cells from input adata
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            adata = anndata.read_h5ad(filename=filepath)

        return self.annotate_anndata(
            adata=adata,
            chunk_size=chunk_size,
            cas_model_name=cas_model_name,
            count_matrix_name=count_matrix_name,
            feature_ids_column_name=feature_ids_column_name,
            include_dev_metadata=include_dev_metadata,
        )

    def annotate_10x_h5_file(
        self,
        filepath: str,
        chunk_size: int = 2000,
        cas_model_name: str = "default",
        count_matrix_name: str = "X",
        feature_ids_column_name: str = "index",
        include_dev_metadata: bool = False,
    ) -> t.List[t.Dict[str, t.Any]]:
        """
        Parse the 10x 'h5' matrix and apply the :meth:`annotate_anndata` method to it.

        :param filepath: Filepath of the local 'h5' matrix
        :param chunk_size: Size of chunks to split on
        :param cas_model_name: Model name to use for annotation. |br|
            `Allowed Values:` Model name from the :attr:`allowed_models_list` list or ``"default"``
            keyword, which refers to the default selected model in the Cellarium backend. |br|
            `Default:` ``"default"``
        :param count_matrix_name:  Where to obtain a feature expression count matrix from. |br|
            `Allowed Values:` Choice of either ``"X"``  or ``"raw.X"`` in order to use ``adata.X`` or ``adata.raw.X``,
             respectively |br|
            `Default:` ``"X"``
        :param feature_ids_column_name: Column name where to obtain Ensembl feature ids. |br|
            `Allowed Values:` A value from ``adata.var.columns`` or ``"index"`` keyword, which refers to index
            column. |br|
            `Default:` ``"index"``
        :param include_dev_metadata: Boolean indicating whether to include a breakdown of the number of cells by dataset
        :return: A list of dictionaries with annotations for each of the cells from input adata
        """
        adata = _io.read_10x_h5(filepath)

        return self.annotate_anndata(
            adata=adata,
            chunk_size=chunk_size,
            cas_model_name=cas_model_name,
            count_matrix_name=count_matrix_name,
            feature_ids_column_name=feature_ids_column_name,
            include_dev_metadata=include_dev_metadata,
        )
