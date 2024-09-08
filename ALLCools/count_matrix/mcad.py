import pathlib
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache

import anndata
import numpy as np
import pandas as pd
import pysam
from scipy import stats
from scipy.sparse import csr_matrix

from .._doc import *
from ..utilities import parse_mc_pattern


def _read_region_bed(bed_path):
    region_bed = pd.read_csv(bed_path, sep="\t", header=None, index_col=3)
    region_bed.index.name = "region"
    region_bed.columns = ["chrom", "start", "end"]
    return region_bed


@lru_cache(99999)
def bin_sf(cov, mc, p):
    if cov > mc:
        return stats.binom(cov, p).sf(mc)
    else:
        # cov == mc, sf = 0
        return 0


def _count_single_allc(allc_path, bed_path, mc_pattern, output_dir, p=None, cutoff=0.9, reverse_value=False):
    patterns = parse_mc_pattern(mc_pattern)
    region_bed = _read_region_bed(bed_path)

    # bin raw counts
    with pysam.TabixFile(allc_path, "r") as allc:
        records = []  # list of [mc, cov, region_idx]
        for idx, (_, (chrom, start, end)) in enumerate(region_bed.iterrows()):
            total_mc = 0
            total_cov = 0
            try:
                iterator = allc.fetch(chrom, start, end)
            except ValueError:
                # in low coverage cells, allc file might miss a whole chromosome,
                # this cause value error saying "could not create iterator for region"
                continue
            for line in iterator:
                chrom, pos, _, context, mc, cov, _ = line.split("\t")
                if context in patterns:
                    total_mc += int(mc)
                    total_cov += int(cov)
            if total_cov > 0:
                records.append([idx, total_mc, total_cov])
        bin_counts = pd.DataFrame(records, columns=["idx", "mc", "cov"]).set_index("idx")

    # calculate binom sf (1-cdf) value, hypo bins are close to 1, hyper bins are close to 0
    if p is None:
        mc_sum, cov_sum = bin_counts.sum()
        p = mc_sum / (cov_sum + 0.000001)  # prevent empty allc error
    pv = bin_counts.apply(lambda x: bin_sf(x["cov"], x["mc"], p), axis=1).astype("float16")
    if reverse_value:
        # use cdf instead of sf when looking for hyper methylation
        pv = 1 - pv
    pv = pv[pv > cutoff]  # get rid of most hyper bins
    pv.to_hdf(f"{output_dir}/{pathlib.Path(allc_path).name}.hdf", key="data")
    return


@doc_params(
    allc_table_doc=allc_table_doc,
    bed_path_doc=region_bed_path_mcad_doc,
    cpu_doc=cpu_basic_doc,
    mc_context_doc=mc_context_mcad_doc,
)
def generate_mcad(
    allc_table,
    bed_path,
    output_prefix,
    mc_context,
    global_path=None,
    cpu=1,
    cleanup=True,
    cutoff=0.9,
    reverse_value=False,
):
    """
    Generate MCAD from ALLC files.

    Parameters
    ----------
    allc_table
        {allc_table_doc}
    bed_path
        {bed_path_doc}
    cpu
        {cpu_doc}
    output_prefix
        Output prefix of the MCAD, a suffix ".mcad" will be added.
    mc_context
        {mc_context_doc}
    global_path
        Path to a global MC table, if provided, the global MC will be used to calculate the p value
    cleanup
        Whether remove temp files or not
    cutoff
        Values smaller than cutoff will be stored as 0, which reduces the file size
    reverse_value
        If true, use cdf instead of sf to make hyper-methylation events having higher values
    """
    # validate
    if (cutoff < 0) or (cutoff > 1):
        raise ValueError(f"Cutoff must between 0 to 1, got {cutoff}.")

    # allc table has 2 columns: cell_id \t allc_path
    allc_paths = pd.read_csv(allc_table, sep="\t", index_col=0, header=None).squeeze()
    allc_paths.index.name = "cell"

    # temp dir
    _name = pathlib.Path(output_prefix).name
    temp_dir = pathlib.Path(f"{_name}_pv_temp")
    temp_dir.mkdir(exist_ok=True)

    if global_path:
        global_mc = pd.read_csv(global_path, sep="\t", index_col=0, header=None).squeeze()
        global_mc.index.name = "cell"

    # calculating individual cells
    with ProcessPoolExecutor(cpu) as executor:
        futures = {}
        allc_path_idy = {}
        for idy, (cell_id, allc_path) in enumerate(allc_paths.items()):
            allc_path_idy[pathlib.Path(allc_path).name] = idy
            output_path = temp_dir / f"{pathlib.Path(allc_path).name}.hdf"
            if output_path.exists():
                continue
            future = executor.submit(
                _count_single_allc,
                allc_path=allc_path,
                bed_path=bed_path,
                mc_pattern=mc_context,
                output_dir=temp_dir,
                p=global_mc.loc[cell_id],
                cutoff=cutoff,
                reverse_value=reverse_value,
            )
            futures[future] = cell_id

        for future in as_completed(futures):
            cell_id = futures[future]
            print(f"{cell_id} returned.")
            future.result()

    # aggregate all the cells
    print("Aggregate cells into adata")
    total_idx = []
    total_idy = []
    total_data = []
    for path in temp_dir.glob("*hdf"):
        idy = allc_path_idy[path.name[:-4]]
        pv = pd.read_hdf(path)
        if pv.size == 0:
            # no sig result
            continue
        total_idx.append(pv.index.values)
        total_idy.append([idy] * pv.size)
        total_data.append(pv.values)
    # the cell by region matrix
    region_bed = _read_region_bed(bed_path)
    _data = csr_matrix(
        (
            np.concatenate(total_data),
            (np.concatenate(total_idy), np.concatenate(total_idx)),
        ),
        shape=(len(allc_path_idy), region_bed.shape[0]),
    )

    # save the data as anndata
    adata = anndata.AnnData(_data, obs=pd.DataFrame([], index=allc_paths.index), var=region_bed)
    adata.X = adata.X.astype("float16")
    if str(output_prefix)[-5:] in {".mcad", ".h5ad"}:
        output_h5ad_path = output_prefix
    else:
        output_h5ad_path = f"{output_prefix}.mcad"
    adata.write_h5ad(pathlib.Path(output_h5ad_path))

    # remove temp
    if cleanup:
        subprocess.run(["rm", "-rf", str(temp_dir)])
    return
