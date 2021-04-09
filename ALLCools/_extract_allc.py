from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from subprocess import run
from typing import Union, Tuple, Callable

import pandas as pd

from ._doc import *
from ._open import open_allc, open_gz
from .utilities import tabix_allc, parse_mc_pattern, parse_chrom_size, genome_region_chunks, binary_count


def _merge_cg_strand(in_path, out_path):
    """
    Merge strand after extract context step in extract_allc (and only apply on CG), so no need to check context.
    """
    prev_line = None
    cur_chrom = None

    with open_allc(in_path) as allc, \
            open_allc(out_path, 'w') as out_allc:
        for line in allc:
            cur_line = line.strip('\n').split('\t')
            if cur_line[0] != cur_chrom:
                if prev_line is not None:
                    out_allc.write('\t'.join(prev_line) + '\n')
                prev_line = cur_line
                cur_chrom = cur_line[0]
                continue
            if prev_line is None:
                prev_line = cur_line
                continue
            else:
                # pos should be continuous, strand should be reverse
                if int(prev_line[1]) + 1 == int(cur_line[1]) and prev_line[2] != cur_line[2]:
                    new_line = prev_line[:4] + [str(int(prev_line[4]) + int(cur_line[4])),
                                                str(int(prev_line[5]) + int(cur_line[5])), '1']
                    prev_line = None
                # otherwise, only write and update prev_line
                else:
                    if prev_line[2] == '-':
                        # change all the '-' strand to '+' strand
                        new_line = [prev_line[0], str(int(prev_line[1]) - 1), '+'] + prev_line[3:]
                    else:
                        new_line = prev_line.copy()
                    prev_line = cur_line
                out_allc.write('\t'.join(new_line) + '\n')
    return


def _check_strandness_parameter(strandness) -> str:
    strandness = str(strandness).lower()
    if strandness in {'both', 'b'}:
        return 'Both'
    elif strandness in {'merge', 'mergetmp', 'm'}:
        # first getting both, deal with strand merge later
        return 'MergeTmp'
    elif strandness in {'split', 's'}:
        return 'Split'
    else:
        raise ValueError(f'Unknown value for strandness: {strandness}')


def _check_out_format_parameter(out_format, binarize=False) -> Tuple[str, Callable[[list], str]]:
    if binarize:
        def _extract_allc_format(allc_line_list):
            # keep allc format
            # mc and cov is binarized
            allc_line_list[4], allc_line_list[5] = \
                binary_count(int(allc_line_list[4]), int(allc_line_list[5]))
            return '\t'.join(map(str, allc_line_list))

        def _extract_bed5_format(allc_line_list):
            # only chrom, pos, pos, mc, cov
            # mc and cov is binarized
            allc_line_list[4], allc_line_list[5] = \
                binary_count(int(allc_line_list[4]), int(allc_line_list[5]))
            allc_line_list = [allc_line_list[i] for i in [0, 1, 1, 4, 5]]
            return '\t'.join(map(str, allc_line_list)) + '\n'
    else:
        def _extract_allc_format(allc_line_list):
            # keep allc format
            return '\t'.join(allc_line_list)

        def _extract_bed5_format(allc_line_list):
            # only chrom, pos, pos, mc, cov
            allc_line_list = [allc_line_list[i] for i in [0, 1, 1, 4, 5]]
            return '\t'.join(allc_line_list) + '\n'

    out_format = str(out_format).lower()
    if out_format == 'allc':
        return 'allc.tsv.gz', _extract_allc_format
    elif out_format == 'bed5':
        return 'bed5.bed.gz', _extract_bed5_format
    else:
        raise ValueError(f'Unknown value for out_format: {out_format}')


def _merge_gz_files(file_list, output_path):
    """
    Merge the small chunk files generated by _extract_allc_parallel, remove the small files after merge
    """
    with open(output_path + 'tmp', 'w') as out_f:
        for file_path in file_list:
            with open_gz(file_path, 'rt') as f:
                out_f.write(f.read())
            run(['rm', '-f', file_path])
    run(['bgzip', output_path + 'tmp'], check=True)
    run(['mv', '-f', output_path + 'tmp.gz', output_path], check=True)
    return output_path


def _extract_allc_parallel(allc_path, output_prefix, mc_contexts, strandness, output_format,
                           chrom_size_path, cov_cutoff, cpu, chunk_size=100000000, tabix=True):
    """
    Parallel extract_allc on region level
    Then parallel merge region chunk files to the final output in order
    Same input output as extract_allc, but will generate a bunch of small files during running
    Don't use this on small files
    """
    output_prefix = output_prefix.rstrip('.')
    regions = genome_region_chunks(chrom_size_path=chrom_size_path,
                                   bin_length=chunk_size,
                                   combine_small=True)
    future_dict = {}
    with ProcessPoolExecutor(cpu) as executor:
        for chunk_id, region in enumerate(regions):
            future = executor.submit(extract_allc,
                                     allc_path=allc_path,
                                     output_prefix=output_prefix + f'.{chunk_id}.',
                                     mc_contexts=mc_contexts,
                                     strandness=strandness,
                                     output_format=output_format,
                                     chrom_size_path=chrom_size_path,
                                     region=region,
                                     cov_cutoff=cov_cutoff,
                                     cpu=1,
                                     tabix=False)
            future_dict[future] = chunk_id

        output_records = {}
        for future in as_completed(future_dict):
            output_path_dict = future.result()
            chunk_id = future_dict[future]
            output_records[chunk_id] = output_path_dict

        # agg chunk_output
        records = []
        for chunk_id, paths_dict in output_records.items():
            for (mc_context, strandness, out_suffix), path in paths_dict.items():
                records.append([path, chunk_id, mc_context, strandness, out_suffix])
        total_output_df = pd.DataFrame(records, columns=['path', 'chunk_id',
                                                         'mc_context', 'strandness',
                                                         'out_suffix'])

        real_out_paths_dict = {}
        need_tabix = []

    # TODO this can be much more improved, learn how cutadapt do parallel read and write!

    with ProcessPoolExecutor(cpu) as merge_executor:
        futures = []
        for (mc_context, strandness, out_suffix), sub_df in total_output_df.groupby(
                ['mc_context', 'strandness', 'out_suffix']):
            ordered_index = sub_df['chunk_id'].astype(int).sort_values().index
            ordered_file_list = sub_df.loc[ordered_index, 'path'].tolist()
            real_file_path = f'{output_prefix}.{mc_context}-{strandness}.{out_suffix}'

            real_out_paths_dict[(mc_context, strandness, out_suffix)] = real_file_path

            if tabix and 'allc' in out_suffix:
                need_tabix.append(real_file_path)
            future = merge_executor.submit(_merge_gz_files,
                                           file_list=ordered_file_list,
                                           output_path=real_file_path)
            futures.append(future)
        for future in as_completed(futures):
            future.result()

    if tabix:
        with ProcessPoolExecutor(cpu) as tabix_executor:
            futures = []
            for path in need_tabix:
                future = tabix_executor.submit(tabix_allc, path)
                futures.append(future)
            for future in as_completed(futures):
                future.result()

    return real_out_paths_dict


@doc_params(allc_path_doc=allc_path_doc,
            mc_contexts_doc=mc_contexts_doc,
            cov_cutoff_doc=cov_cutoff_doc,
            chrom_size_path_doc=chrom_size_path_doc,
            strandness_doc=strandness_doc,
            region_doc=region_doc,
            cpu_basic_doc=cpu_basic_doc,
            binarize_doc=binarize_doc)
def extract_allc(allc_path: str,
                 output_prefix: str,
                 mc_contexts: Union[str, list],
                 chrom_size_path: str,
                 strandness: str = 'both',
                 output_format: str = 'allc',
                 region: str = None,
                 cov_cutoff: int = 9999,
                 tabix: bool = True,
                 cpu=1,
                 binarize=False):
    """\
    Extract information (strand, context) from 1 ALLC file.
    Save to several different format.

    Parameters
    ----------
    allc_path
        {allc_path_doc}
    output_prefix
        Path prefix of the output ALLC file.
    mc_contexts
        {mc_contexts_doc}
    strandness
        {strandness_doc}
    output_format
        Output format of extracted information, possible values are:
        1. allc: keep the allc format
        2. bed5: 5-column bed format, chrom, pos, pos, mc, cov
    chrom_size_path
        {chrom_size_path_doc}
        If chrom_size_path provided, will use it to extract ALLC with chrom order,
        but if region provided, will ignore this.
    region
        {region_doc}
    cov_cutoff
        {cov_cutoff_doc}
    tabix
        Whether to generate tabix if format is ALLC, only set this to False from _extract_allc_parallel
    cpu
        {cpu_basic_doc}
        This function parallel on region level and will generate a bunch of small files if cpu > 1.
        Do not use cpu > 1 for single cell region count. For single cell data, parallel on cell level is better.
    binarize
        {binarize_doc}
    Returns
    -------
    A list of output file paths, not include index files.
    """
    # TODO write test
    parallel_chunk_size = 100000000

    # determine region and parallel
    parallel = False
    if region is None:
        if chrom_size_path is not None:
            chrom_dict = parse_chrom_size(chrom_size_path)
            region = ' '.join(chrom_dict.keys())
            if cpu > 1:
                parallel = True

    # prepare params
    output_prefix = output_prefix.rstrip('.')
    if isinstance(mc_contexts, str):
        mc_contexts = mc_contexts.split(' ')
    mc_contexts = list(set(mc_contexts))
    strandness = _check_strandness_parameter(strandness)
    out_suffix, line_func = _check_out_format_parameter(output_format, binarize=binarize)

    # because mc_contexts can overlap (e.g. CHN, CAN)
    # each context may associate to multiple handle
    context_handle = defaultdict(list)
    handle_collect = []
    output_path_collect = {}
    for mc_context in mc_contexts:
        parsed_context_set = parse_mc_pattern(mc_context)
        if strandness == 'Split':
            file_path = output_prefix + f'.{mc_context}-Watson.{out_suffix}'
            output_path_collect[(mc_context, 'Watson', out_suffix)] = file_path
            w_handle = open_allc(file_path, 'w')
            handle_collect.append(w_handle)

            file_path = output_prefix + f'.{mc_context}-Crick.{out_suffix}'
            output_path_collect[(mc_context, 'Crick', out_suffix)] = file_path
            c_handle = open_allc(file_path, 'w')
            handle_collect.append(c_handle)
            for mc_pattern in parsed_context_set:
                # handle for Watson/+ strand
                context_handle[(mc_pattern, '+')].append(w_handle)
                # handle for Crick/- strand
                context_handle[(mc_pattern, '-')].append(c_handle)
        else:
            # handle for both strand
            file_path = output_prefix + f'.{mc_context}-{strandness}.{out_suffix}'
            if strandness == 'MergeTmp':
                output_path_collect[(mc_context, 'Merge', out_suffix)] = \
                    output_prefix + f'.{mc_context}-Merge.{out_suffix}'
            else:
                output_path_collect[(mc_context, strandness, out_suffix)] = file_path
            _handle = open_allc(file_path, 'w')
            handle_collect.append(_handle)
            for mc_pattern in parsed_context_set:
                context_handle[mc_pattern].append(_handle)

    # determine parallel or not
    cpu = int(cpu)
    if parallel:
        print('Parallel extract ALLC')
        if strandness == 'MergeTmp':
            strandness = 'Merge'
        return _extract_allc_parallel(allc_path=allc_path,
                                      output_prefix=output_prefix,
                                      mc_contexts=mc_contexts,
                                      strandness=strandness,
                                      output_format=output_format,
                                      chrom_size_path=chrom_size_path,
                                      cov_cutoff=cov_cutoff,
                                      cpu=cpu,
                                      chunk_size=parallel_chunk_size,
                                      tabix=tabix)

    # split file first
    # strandness function
    with open_allc(allc_path, region=region) as allc:
        if strandness == 'Split':
            for line in allc:
                cur_line = line.split('\t')
                if int(cur_line[5]) > cov_cutoff:
                    continue
                try:
                    # key is (context, strand)
                    [h.write(line_func(cur_line)) for h in context_handle[(cur_line[3], cur_line[2])]]
                except KeyError:
                    continue
        else:
            for line in allc:
                cur_line = line.split('\t')
                if int(cur_line[5]) > cov_cutoff:
                    continue
                try:
                    # key is context
                    [h.write(line_func(cur_line)) for h in context_handle[cur_line[3]]]
                except KeyError:
                    continue
    for handle in handle_collect:
        handle.close()

    for mc_context in mc_contexts:
        # tabix ALLC file
        if strandness == 'Split':
            in_path = output_prefix + f'.{mc_context}-Watson.{out_suffix}'
            if tabix:
                tabix_allc(in_path)
            in_path = output_prefix + f'.{mc_context}-Crick.{out_suffix}'
            if tabix:
                tabix_allc(in_path)
        elif strandness == 'MergeTmp':
            in_path = output_prefix + f'.{mc_context}-{strandness}.{out_suffix}'
            if ('CG' in mc_context) and ('allc' in out_suffix):
                out_path = output_prefix + f'.{mc_context}-Merge.{out_suffix}'
                _merge_cg_strand(in_path, out_path)
                run(['rm', '-f', in_path], check=True)
            else:
                # for non-CG, there is no need to merge strand
                out_path = output_prefix + f'.{mc_context}-Both.{out_suffix}'
                run(['mv', in_path, out_path], check=True)
            if tabix:
                tabix_allc(out_path)
        else:
            in_path = output_prefix + f'.{mc_context}-{strandness}.{out_suffix}'
            if tabix:
                tabix_allc(in_path)

    # return a dict, key is (mc_context, strandness, out_suffix), value is file path
    return output_path_collect
