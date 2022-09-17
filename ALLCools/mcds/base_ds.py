import pathlib

import numpy as np
import pandas as pd
import xarray as xr

from ALLCools.utilities import parse_mc_pattern


def _chunk_pos_to_bed_df(chrom, chunk_pos):
    records = []
    for i in range(len(chunk_pos) - 1):
        start = chunk_pos[i]
        end = chunk_pos[i + 1]
        records.append([chrom, start, end])
    return pd.DataFrame(records, columns=["chrom", "start", "end"])


def _filter_pos_by_region_df(region_df, pos_idx):
    n_rows = region_df.shape[0]
    if n_rows == 0:
        return np.array([])

    region_row = 0
    _, cur_start, cur_end = region_df.iloc[region_row]
    pos_list = []
    for pos in pos_idx:
        if pos <= cur_start:
            continue
        else:
            if pos > cur_end:
                region_row += 1
                if region_row >= n_rows:
                    break
                # get next region
                _, cur_start, cur_end = region_df.iloc[region_row]

            # add pos if inside region
            if cur_start < pos <= cur_end:
                pos_list.append(pos)
    pos_arr = np.array(pos_list)
    return pos_arr


class Codebook(xr.DataArray):
    """The Codebook data array records methyl-cytosine context in genome."""

    __slots__ = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # in-memory cache for the positions of cytosines matching the pattern
        self.attrs["__mc_pos_cache"] = {}
        self.attrs["__mc_pos_bool_cache"] = {}

        # the continuity of codebook is determined by occurrence of pos coords
        # and should not be changed
        self.attrs["__continuity"] = False if "pos" in self.coords else True

    @property
    def continuity(self):
        """Whether the codebook is continuous or not."""
        return self.attrs["__continuity"]

    @property
    def mc_type(self):
        """The type of methyl-cytosine context."""
        return self.get_index("mc_type")

    @property
    def c_pos(self):
        """The positions of cytosines in mC type."""
        return self.attrs["c_pos"]

    @property
    def context_size(self):
        """The size of context in mC type."""
        return self.attrs["context_size"]

    def _validate_mc_pattern(self, mc_pattern):
        mc_pattern = mc_pattern.upper()

        if len(mc_pattern) != self.context_size:
            raise ValueError(
                f"The length of mc_pattern {len(mc_pattern)} is not equal to context_size {self.context_size}."
            )
        if mc_pattern[self.c_pos] != "C":
            raise ValueError(f"The c_pos position {self.c_pos} in mc_pattern {mc_pattern} is not a cytosine.")

        return mc_pattern

    def get_mc_pos_bool(self, mc_pattern):
        """Get the boolean array of cytosines matching the pattern."""
        mc_pattern = self._validate_mc_pattern(mc_pattern)

        if mc_pattern in self.attrs["__mc_pos_bool_cache"]:
            return self.attrs["__mc_pos_bool_cache"][mc_pattern]
        else:
            if mc_pattern is None:
                # get all mc types
                judge = np.ones_like(self.mc_type, dtype=bool)
            else:
                # get mc types matching the pattern
                judge = self.mc_type.isin(parse_mc_pattern(mc_pattern))
            # value can be -1, 0, 1, only 0 is False, -1 and 1 are True
            _bool = self.sel(mc_type=judge).sum(dim="mc_type").values.astype(bool)
            self.attrs["__mc_pos_bool_cache"][mc_pattern] = _bool
            return _bool

    def get_mc_pos(self, mc_pattern, offset=None):
        """Get the positions of mc types matching the pattern."""
        mc_pattern = self._validate_mc_pattern(mc_pattern)

        if mc_pattern in self.attrs["__mc_pos_cache"]:
            _pos = self.attrs["__mc_pos_cache"][mc_pattern]
        else:
            _bool = self.get_mc_pos_bool(mc_pattern)

            if self.continuity:
                _pos = self.coords["pos"].values[_bool]
            else:
                _pos = np.where(_bool)[0]
            self.attrs["__mc_pos_cache"][mc_pattern] = _pos

        if offset is not None:
            _pos = _pos.copy()
            _pos += offset
        return _pos


class BaseDSChrom(xr.Dataset):
    """The BaseDS class for data within single chromosome."""

    __slots__ = ()

    def __init__(self, dataset, coords=None, attrs=None):
        if isinstance(dataset, xr.Dataset):
            data_vars = dataset.data_vars
            coords = dataset.coords if coords is None else coords
            attrs = dataset.attrs if attrs is None else attrs
        else:
            data_vars = dataset
        super().__init__(data_vars=data_vars, coords=coords, attrs=attrs)

        # continuity is set to True by default
        self.continuity = True
        self.offset = 0
        return

    @property
    def continuity(self):
        """
        The continuity of pos dimension.

        the BaseDSChrom has tow mode on the position dimension:
        1. "continuous" mode: the position dimension is continuous
        and all bases (including non-cytosines) are recorded.
        In this mode, the position dimension do not have coordinates, the index is genome position.
        2. "discrete" mode: the position dimension is discrete due to some discontinuous selection.
        In this mode, the position dimension has coordinates.
        """
        return self.attrs["__continuity"]

    @continuity.setter
    def continuity(self, value):
        """Set the continuity of pos dimension."""
        if not value:
            assert "pos" in self.coords, (
                "The position dimension is set to discontinuous, " "but the pos coords is missing."
            )
            # when continuity is set to False, the offset is set to None
            self.offset = None
            self.attrs["__continuity"] = False
        else:
            self.attrs["__continuity"] = True

    @property
    def offset(self):
        """The offset of the position dimension, only valid when continuity is True."""
        offset = self.attrs["__offset"]
        if offset is None:
            offset = 0
        return offset

    @offset.setter
    def offset(self, value):
        """Set the offset of the position dimension."""
        if value is not None and not self.continuity:
            raise ValueError("The offset is only valid when the position dimension is continuous.")
        self.attrs["__offset"] = value

    def clear_attr_cache(self):
        """Clear the attr cache."""
        for attr in list(self.attrs.keys()):
            if str(attr).startswith("__"):
                del self.attrs[attr]

    def _continuous_pos_selection(self, start, end):
        """Select the positions to create a continuous BaseDSChrom."""
        # for continuous mode, the pos should have an offset to convert to genome position
        if not self.continuity:
            raise ValueError("The position dimension is not continuous, unable to perform _continuous_pos_selection.")

        if start is not None or end is not None:
            if start is not None:
                start -= self.offset
            if end is not None:
                end -= self.offset
            obj = self.sel(pos=slice(start, end, None))
            if start is not None:
                obj.offset = start
            return obj
        else:
            return self

    def _discontinuous_pos_selection(self, pos_sel=None, idx_sel=None):
        """
        Select the positions to create a discontinuous BaseDSChrom.

        Parameters
        ----------
        pos_sel :
            using genome position to select the positions,
            for continuous mode, the pos should have an offset to convert to idx position
        idx_sel :
            using idx position to select the positions

        Returns
        -------
        BaseDSChrom
        """
        # once the pos is selected, the ds is not continuous anymore
        # one must set the pos coords and set the continuity to False
        if idx_sel is not None and pos_sel is not None:
            raise ValueError("Only one of idx_sel and pos_sel can be specified.")
        elif idx_sel is not None:
            pass
        elif pos_sel is not None:
            if self.continuity:
                # da is continuous, convert pos to idx
                idx_sel = pos_sel - self.offset
            else:
                # da is not continuous, treat pos_sel as idx_sel
                idx_sel = pos_sel
        else:
            raise ValueError("One of idx_sel or pos_sel must be specified.")

        if self.continuity:
            # if the ds was continuous, add offset to the pos coords and turn off continuity
            offset_to_add = self.offset
        else:
            offset_to_add = 0

        ds = self.sel(pos=idx_sel).assign_coords(pos=idx_sel + offset_to_add)
        ds.clear_attr_cache()
        ds.continuity = False
        return ds

    def fetch(self, start=None, end=None, pos_sel=None):
        """
        Fetch the data within the specified region or positions.

        Parameters
        ----------
        start :
            the start position of the region, the resulting BaseDSChrom will be continuous
        end :
            the end position of the region, the resulting BaseDSChrom will be continuous
        pos_sel :
            the positions to select, the resulting BaseDSChrom will be discontinuous

        Returns
        -------
        BaseDSChrom
        """
        if start is not None or end is not None:
            if pos_sel is not None:
                raise ValueError("Only one of start/end and pos_sel can be specified.")
            return self._continuous_pos_selection(start=start, end=end)
        else:
            return self._discontinuous_pos_selection(pos_sel=pos_sel)

    @staticmethod
    def _xarray_open(path):
        multi = False
        if isinstance(path, (str, pathlib.Path)):
            if "*" in str(path):
                multi = True
        else:
            if len(path) > 1:
                multi = True
            else:
                path = path[0]

        if multi:
            ds = xr.open_mfdataset(path, concat_dim="sample_id", combine="nested", engine="zarr", decode_cf=False)
        else:
            ds = xr.open_zarr(path, decode_cf=False)
        return ds

    @classmethod
    def open(cls, path, start=None, end=None, codebook_path=None):
        """
        Open a BaseDSChrom object from a zarr path.

        If start and end are not None, only the specified region will be opened.

        Parameters
        ----------
        path
            The zarr path to the chrom dataset.
        start
            The start position of the region to be opened.
        end
            The end position of the region to be opened.
        codebook_path
            The path to the codebook file if the BaseDS does not have a codebook.
            Codebook contexts, c_pos, and shape must be compatible with the BaseDS.

        Returns
        -------
        BaseDSChrom
        """
        _zarr_obj = cls._xarray_open(path)

        if "codebook" not in _zarr_obj.data_vars:
            if codebook_path is None:
                raise ValueError("The BaseDS does not have a codebook, but no codebook_path is specified.")
            _cb = xr.open_zarr(codebook_path, decode_cf=False)["codebook"]
            # validate _cb attrs compatibility
            flag = True
            _cb_mc_types = _cb.get_index("mc_type").values
            _obj_mc_types = _zarr_obj.get_index("mc_type").values
            # noinspection PyUnresolvedReferences
            _diff = (_cb_mc_types != _obj_mc_types).sum()
            if _diff > 0:
                flag = False
                print("The codebook mc_types are not compatible with the BaseDS.")
            if _cb.shape[0] != _zarr_obj["data"].shape[0]:
                flag = False
                print("The codebook shape is not compatible with the BaseDS.")
            if not flag:
                raise ValueError("The BaseDS and codebook are not compatible.")
            _zarr_obj["codebook"] = _cb

        _obj = cls(_zarr_obj)
        _obj = _obj._continuous_pos_selection(start, end)
        return _obj

    @property
    def chrom(self):
        """The chromosome name."""
        return self.attrs["chrom"]

    @property
    def chrom_size(self):
        """The chromosome size."""
        return self.attrs["chrom_size"]

    @property
    def obs_dim(self):
        """The observation dimension name."""
        return self.attrs["obs_dim"]

    @property
    def obs_size(self):
        """The observation size."""
        return self.attrs["obs_size"]

    @property
    def obs_names(self):
        """The observation names."""
        return self.get_index(self.obs_dim)

    @property
    def mc_types(self):
        """The methyl-cytosine types."""
        return self.get_index("mc_type")

    @property
    def chrom_chunk_pos(self):
        """The chromosome chunk position."""
        return self.get_index("chunk_pos")

    @property
    def chrom_chunk_bed_df(self) -> pd.DataFrame:
        """The chromosome chunk bed dataframe."""
        chunk_pos = self.chrom_chunk_pos
        bed = _chunk_pos_to_bed_df(self.chrom, chunk_pos)
        return bed

    @property
    def codebook(self) -> Codebook:
        """Get the codebook data array."""
        # catch the codebook in the attrs, only effective in memory
        if "__cb_obj" not in self.attrs:
            self.attrs["__cb_obj"] = Codebook(self["codebook"])
            self.attrs["__cb_obj"].attrs["c_pos"] = self.attrs["c_pos"]
            self.attrs["__cb_obj"].attrs["context_size"] = self.attrs["context_size"]
        return self.attrs["__cb_obj"]

    @property
    def cb(self) -> Codebook:
        """Alias for codebook."""
        return self.codebook

    def select_mc_type(self, pattern):
        cb = self.codebook
        pattern_pos = cb.get_mc_pos(pattern)

        ds = self._discontinuous_pos_selection(idx_sel=pattern_pos)
        return ds

    @property
    def pos_index(self):
        """The position index."""
        return self.get_index("pos")

    def get_region_ds(
        self,
        mc_type,
        bin_size=None,
        regions=None,
        region_name=None,
        region_chunks=10000,
        region_start=None,
        region_end=None,
    ):
        """
        Get the region dataset.

        Parameters
        ----------
        mc_type
            The mc_type to be selected.
        bin_size
            The bin size to aggregate BaseDS to fix-sized regions.
        regions
            The regions dataframe containing three columns: chrom, start, end.
            The index will be used as the region names.
        region_name
            The dimension name of the regions.
        region_chunks
            The chunk size of the region dim in result dataset.
        region_start
            The start position of the region to be selected.
        region_end
            The end position of the region to be selected.

        Returns
        -------
        BaseDSChrom
        """
        if bin_size is None and regions is None:
            raise ValueError("One of bin_size or regions must be specified.")

        if bin_size is not None:
            assert bin_size > 1, "bin_size must be greater than 1."
            all_idx = self.get_index("pos")
            region_start = all_idx.min() + self.offset if region_start is None else region_start
            region_end = all_idx.max() + self.offset if region_end is None else region_end

        if regions is not None:
            assert regions.shape[1] == 3, "regions must be a 3-column dataframe: chrom, start, end."
            assert regions.shape[0] > 0, "regions must have at least one row."

        # get positions
        pos_idx = self.cb.get_mc_pos(mc_type, offset=self.offset)
        if regions is not None:
            pos_idx = _filter_pos_by_region_df(region_df=regions, pos_idx=pos_idx)

        base_ds = self._discontinuous_pos_selection(pos_sel=pos_idx)

        # prepare regions
        if regions is not None:
            bins = regions.iloc[:, 1].tolist() + [regions.iloc[-1, 2]]
            labels = regions.index
            region_name = regions.index.name
        else:
            bins = []
            for i in range(0, self.chrom_size, bin_size):
                if i < region_start or i > region_end:
                    continue
                bins.append(i)
            if bins[-1] < region_end:
                bins.append(region_end)
            if bins[0] > region_start:
                bins.insert(0, region_start)

            labels = []
            for start in bins[:-1]:
                labels.append(start)

        # no CpG selected
        if pos_idx.size == 0:
            # create an empty region_ds
            region_ds = base_ds["data"].rename({"pos": "pos_bins"})
            region_ds = region_ds.reindex({"pos_bins": labels}, fill_value=0)
        else:
            region_ds = (
                base_ds["data"]
                .groupby_bins(
                    group="pos",
                    bins=bins,
                    right=True,
                    labels=labels,
                    precision=3,
                    include_lowest=True,
                    squeeze=True,
                    restore_coord_dims=False,
                )
                .sum(dim="pos")
                .chunk({"pos_bins": region_chunks})
            )

        if region_name is not None:
            region_ds = region_ds.rename({"pos_bins": region_name})

        region_ds = xr.Dataset({"data": region_ds}).fillna(0)
        return region_ds

    def call_dms(
        self,
        groups,
        output_path=None,
        mcg_pattern="CGN",
        n_permute=3000,
        alpha=0.01,
        max_row_count=50,
        max_total_count=3000,
        filter_sig=True,
        merge_strand=True,
        estimate_p=True,
        cpu=1,
        **output_kwargs,
    ):
        """
        Call DMS for a genomic region.

        Parameters
        ----------
        groups :
            Grouping information for the samples.
            If None, perform DMS test on all samples in the BaseDS.
            If provided, first group the samples by the group information, then perform DMS test on each group.
            Samples not occur in the group information will be ignored.
        output_path :
            Path to the output DMS dataset.
            If provided, the result will be saved to disk.
            If not, the result will be returned.
        mcg_pattern :
            Pattern of the methylated cytosine, default is "CGN".
        n_permute :
            Number of permutation to perform.
        alpha :
            Minimum p-value/q-value to consider a site as significant.
        max_row_count :
            Maximum number of base counts for each row (sample) in the DMS input count table.
        max_total_count :
            Maximum total number of base counts in the DMS input count table.
        estimate_p :
            Whether to estimate p-value by approximate the null distribution of S as normal distribution.
            The resolution of the estimated p-value is much higher than the exact p-value,
            which is necessary for multiple testing correction.
            FDR corrected q-value is also estimated if this option is enabled.
        filter_sig :
            Whether to filter out the non-significant sites in output DMS dataset.
        merge_strand :
            Whether to merge the base counts of CpG sites next to each other.
        cpu :
            Number of CPU to use.
        output_kwargs :
            Keyword arguments for the output DMS dataset, pass to xarray.Dataset.to_zarr.

        Returns
        -------
        xarray.Dataset if output_path is None, otherwise None.
        """
        from ..dmr.call_dms_baseds import call_dms_worker

        # TODO validate if the BaseDS has the required data for calling DMS

        dms_ds = call_dms_worker(
            groups=groups,
            base_ds=self,
            mcg_pattern=mcg_pattern,
            n_permute=n_permute,
            alpha=alpha,
            max_row_count=max_row_count,
            max_total_count=max_total_count,
            estimate_p=estimate_p,
            cpu=cpu,
            chrom=self.chrom,
            filter_sig=filter_sig,
            merge_strand=merge_strand,
            output_path=output_path,
            **output_kwargs,
        )
        return dms_ds


class BaseDS:
    def __init__(self, paths, codebook_path=None):
        """
        A wrapper for one or multiple BaseDS datasets.

        Parameters
        ----------
        paths :
            Path to the BaseDS datasets.
        codebook_path :
            Path to the codebook file.
        """
        self.paths = self._parse_paths(paths)
        self.codebook_path = codebook_path
        self.__base_ds_cache = {}

    @staticmethod
    def _parse_paths(paths):
        import glob

        _paths = []
        if isinstance(paths, str):
            if "*" in paths:
                _paths += list(glob.glob(paths))
            else:
                _paths.append(paths)
        elif isinstance(paths, pathlib.Path):
            _paths.append(str(paths))
        else:
            _paths += list(paths)
        return _paths

    def _get_chrom_paths(self, chrom):
        return [f"{p}/{chrom}" for p in self.paths]

    def fetch(self, chrom, start=None, end=None, pos_sel=None, mc_type=None):
        """
        Fetch a BaseDS for a genomic region or a series of positions.

        Parameters
        ----------
        chrom :
            Chromosome name.
        start :
            Select genomic region by start position.
        end :
            Select genomic region by end position.
        pos_sel :
            Select bases by a list of positions.
        mc_type :
            Methylated cytosine type,
            default is None, a continuous BaseDS region will be returned;
            if provided, a discontinuous BaseDS region containing only the selected cytosine will be returned.

        Returns
        -------
        BaseDSChrom
        """
        if chrom not in self.__base_ds_cache:
            self.__base_ds_cache[chrom] = BaseDSChrom.open(
                path=self._get_chrom_paths(chrom),
                codebook_path=f"{self.codebook_path}/{chrom}",
            )
        _base_ds = self.__base_ds_cache[chrom]

        if start is not None or end is not None or pos_sel is not None:
            _base_ds = _base_ds.fetch(start=start, end=end, pos_sel=pos_sel)

        if mc_type is not None:
            _base_ds = _base_ds.select_mc_type(mc_type)

        return _base_ds
