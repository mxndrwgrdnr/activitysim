# ActivitySim
# See full license in LICENSE.txt.
import logging

import pandas as pd
import numpy as np

from activitysim.core import tracing
from activitysim.core import config
from activitysim.core import pipeline
from activitysim.core import simulate
from activitysim.core import inject
from activitysim.core import logit
from activitysim.core import orca
from activitysim.core import los

from activitysim.core.util import reindex

from activitysim.core.interaction_sample_simulate \
    import interaction_sample_simulate
from activitysim.core.interaction_sample import interaction_sample

from activitysim.core.mem import force_garbage_collect

from . import trip, logsums as logsum

from activitysim.abm.tables.size_terms import tour_destination_size_terms


logger = logging.getLogger(__name__)
DUMP = False


def _od_sample(
        spec_segment_name,
        choosers,
        od_size_terms,
        skims,
        estimator,
        model_settings,
        alt_od_col_name,
        chunk_size,
        trace_label):

    model_spec = simulate.spec_for_segment(model_settings,
                                           spec_id='SAMPLE_SPEC',
                                           segment_name=spec_segment_name,
                                           estimator=estimator)

    logger.info("running %s with %d tours", trace_label, len(choosers))

    sample_size = model_settings['SAMPLE_SIZE']
    if config.setting('disable_destination_sampling', False) or (
            estimator and estimator.want_unsampled_alternatives):
        # FIXME interaction_sample will return unsampled complete alternatives
        # with probs and pick_count
        logger.info((
            "Estimation mode for %s using unsampled alternatives ",
            "short_circuit_choices") % (trace_label,))
        sample_size = 0

    locals_d = {
        'skims': skims
    }
    constants = config.get_model_constants(model_settings)
    if constants is not None:
        locals_d.update(constants)

    choices = interaction_sample(
        choosers,
        alternatives=od_size_terms,
        sample_size=sample_size,
        alt_col_name=alt_od_col_name,
        spec=model_spec,
        skims=skims,
        locals_d=locals_d,
        chunk_size=chunk_size,
        trace_label=trace_label)

    return choices


def od_sample(
        spec_segment_name,
        choosers,
        model_settings,
        network_los,
        od_size_terms,
        estimator,
        chunk_size, trace_label):
    
    origin_col_name = model_settings['ORIG_COL_NAME']
    dest_col_name = model_settings['DEST_COL_NAME']
    alt_dest_col_name = model_settings['ALT_DEST_COL_NAME']

    # create wrapper with keys for this lookup
    # the skims will be available under the name "skims" for any @ expressions
    # skim_origin_col_name = model_settings['CHOOSER_ORIG_COL_NAME']
    # skim_dest_col_name = od_size_terms.index.name
    # # (logit.interaction_dataset suffixes duplicate
    # # chooser column with '_chooser')
    # if (skim_origin_col_name == skim_dest_col_name):
    #     skim_origin_col_name = f'{skim_origin_col_name}_chooser'

    skim_dict = network_los.get_default_skim_dict()
    skims = skim_dict.wrap(origin_col_name, dest_col_name)

    # the name of the od column to be returned in choices
    alt_od_col_name = model_settings['ALT_OD_COL_NAME']
    choices = _od_sample(
        spec_segment_name,
        choosers,
        od_size_terms,
        skims,
        estimator,
        model_settings,
        alt_od_col_name,
        chunk_size,
        trace_label)

    return choices


# temp column names for presampling
DEST_MAZ = 'dest_MAZ'
DEST_TAZ = 'dest_TAZ'

# likewise a temp, but if already in choosers,
# we assume we can use it opportunistically
ORIG_TAZ = 'TAZ'


def map_maz_to_taz(s, network_los):
    maz_to_taz = network_los.maz_taz_df[['MAZ', 'TAZ']].set_index('MAZ').TAZ
    return s.map(maz_to_taz)


def aggregate_size_terms(od_size_terms, network_los, dest_maz_id_col=None, taz_agg_col=DEST_TAZ):
    #
    # aggregate MAZ_size_terms to TAZ_size_terms
    #
    MAZ_size_terms = od_size_terms.copy()

    # add crosswalk DEST_TAZ column to MAZ_size_terms
    if dest_maz_id_col is not None:
        MAZ_size_terms.drop_duplicates(dest_maz_id_col, inplace=True)
        MAZ_size_terms.set_index(dest_maz_id_col, inplace=True)
    MAZ_size_terms[taz_agg_col] = map_maz_to_taz(MAZ_size_terms.index, network_los)

    # aggregate to TAZ
    TAZ_size_terms = MAZ_size_terms.groupby(taz_agg_col).agg({'size_term': 'sum'})
    assert not TAZ_size_terms['size_term'].isna().any()

    #           size_term
    # dest_TAZ
    # 2              45.0
    # 3              44.0
    # 4              59.0

    # add crosswalk DEST_TAZ column to MAZ_size_terms
    # MAZ_size_terms = MAZ_size_terms.sort_values([DEST_TAZ, 'size_term'])  # maybe helpful for debugging
    og_size_term_idx = MAZ_size_terms.index.name
    MAZ_size_terms = MAZ_size_terms[[DEST_TAZ, 'size_term']].reset_index(drop=False)
    MAZ_size_terms = MAZ_size_terms.sort_values([DEST_TAZ, og_size_term_idx]).reset_index(drop=True)

    #       zone_id  dest_TAZ  size_term
    # 0        6097         2       10.0
    # 1       16421         2       13.0
    # 2       24251         3       14.0

    # print(f"TAZ_size_terms ({TAZ_size_terms.shape})\n{TAZ_size_terms}")
    # print(f"MAZ_size_terms ({MAZ_size_terms.shape})\n{MAZ_size_terms}")

    return MAZ_size_terms, TAZ_size_terms


def choose_MAZ_for_TAZ(
        taz_sample, MAZ_size_terms, trace_label,
        dest_maz_id_col=DEST_MAZ,
        unique_alt_id_col=DEST_MAZ
        ):
    """
    Convert taz_sample table with TAZ zone sample choices to a table with a MAZ zone chosen for each TAZ
    choose MAZ probabilistically (proportionally by size_term) from set of MAZ zones in parent TAZ

    Parameters
    ----------
    taz_sample: dataframe with duplicated index <chooser_id_col> and columns: <DEST_TAZ>, prob, pick_count
    MAZ_size_terms: dataframe with unique index and columns: <dest_maz_id_col>, dest_TAZ, size_term

    Returns
    -------
    dataframe with with duplicated index <chooser_id_col> and columns: <DEST_MAZ>, prob, pick_count
    """

    # print(f"taz_sample\n{taz_sample}")
    #           dest_TAZ      prob  pick_count  person_id
    # tour_id
    # 542963          18  0.004778           1      13243
    # 542963          53  0.004224           2      13243
    # 542963          59  0.008628           1      13243

    trace_hh_id = inject.get_injectable("trace_hh_id", None)
    have_trace_targets = trace_hh_id and tracing.has_trace_targets(taz_sample)
    if have_trace_targets:
        trace_label = tracing.extend_trace_label(trace_label, 'choose_MAZ_for_TAZ')

        CHOOSER_ID = taz_sample.index.name  # zone_id for tours, but person_id for location choice
        assert CHOOSER_ID is not None

        # write taz choices, pick_counts, probs
        trace_targets = tracing.trace_targets(taz_sample)
        tracing.trace_df(taz_sample[trace_targets],
                         label=tracing.extend_trace_label(trace_label, 'taz_sample'),
                         transpose=False)


    # if sample_cols_to_keep is not None:
    #     if isinstance(sample_cols_to_keep, str):
    #         sample_cols_to_keep = [sample_cols_to_keep]
    # else:
    #     sample_cols_to_keep = []
    if unique_alt_id_col != DEST_MAZ:
        sample_cols_to_keep = [unique_alt_id_col]
    else:
        sample_cols_to_keep = []

    # redupe taz_sample[[DEST_TAZ, 'prob']] using pick_count to repeat rows
    taz_choices = taz_sample[[DEST_TAZ, 'prob'] + sample_cols_to_keep].reset_index(drop=False)
    taz_choices = taz_choices.reindex(taz_choices.index.repeat(taz_sample.pick_count)).reset_index(drop=True)
    taz_choices = taz_choices.rename(columns={'prob': 'TAZ_prob'})

    # print(f"taz_choices\n{taz_choices}")
    #        tour_id  dest_TAZ  TAZ_prob
    # 0       542963        18  0.004778
    # 1       542963        53  0.004224
    # 2       542963        53  0.004224
    # 3       542963        59  0.008628

    # print(f"MAZ_size_terms\n{MAZ_size_terms}")
    #       zone_id  dest_TAZ  size_term
    # 0        6097         2      7.420
    # 1       16421         2      9.646
    # 2       24251         2     10.904

    # just to make it clear we are siloing choices by chooser_id
    chooser_id_col = taz_sample.index.name  # should be canonical chooser index name (e.g. 'person_id')

    # for random_for_df, we need df with de-duplicated chooser canonical index
    chooser_df = pd.DataFrame(index=taz_sample.index[~taz_sample.index.duplicated()])
    num_choosers = len(chooser_df)
    assert chooser_df.index.name == chooser_id_col

    # to make choices, <taz_sample_size> rands for each chooser (one rand for each sampled TAZ)
    # taz_sample_size will be model_settings['SAMPLE_SIZE'] samples, except if we are estimating
    taz_sample_size = taz_choices.groupby(chooser_id_col)[DEST_TAZ].count().max()

    # taz_choices index values should be contiguous
    assert (taz_choices[chooser_id_col] == np.repeat(chooser_df.index, taz_sample_size)).all()

    # we need to choose a MAZ for each DEST_TAZ choice
    # probability of choosing MAZ based on MAZ size_term fraction of TAZ total
    # there will be a different set (and number) of candidate MAZs for each TAZ
    # (preserve index, which will have duplicates as result of join)
    # maz_sizes.index is the integer offset into taz_choices of the taz for which the maz_size row is a candidate)
    maz_sizes = pd.merge(taz_choices[[chooser_id_col, DEST_TAZ] + sample_cols_to_keep].reset_index(),
                         MAZ_size_terms, how='left', on=DEST_TAZ).set_index('index')

    #         tour_id  dest_TAZ  zone_id  size_term
    # index
    # 0        542963        18      498     12.130
    # 0        542963        18     7696     18.550
    # 0        542963        18    15431      8.678
    # 0        542963        18    21429     29.938
    # 1        542963        53    17563     34.252

    if have_trace_targets:
        # write maz_sizes: maz_sizes[index,tour_id,dest_TAZ,zone_id,size_term]

        maz_sizes_trace_targets = tracing.trace_targets(maz_sizes, slicer=CHOOSER_ID)
        trace_maz_sizes = maz_sizes[maz_sizes_trace_targets]
        tracing.trace_df(trace_maz_sizes,
                         label=tracing.extend_trace_label(trace_label, 'maz_sizes'),
                         transpose=False)

    # number of DEST_TAZ candidates per chooser
    maz_counts = maz_sizes.groupby(maz_sizes.index).size().values

    # max number of MAZs for any TAZ
    max_maz_count = maz_counts.max()

    # offsets of the first and last rows of each chooser in sparse interaction_utilities
    last_row_offsets = maz_counts.cumsum()
    first_row_offsets = np.insert(last_row_offsets[:-1], 0, 0)

    # repeat the row offsets once for each dummy utility to insert
    # (we want to insert dummy utilities at the END of the list of alternative utilities)
    # inserts is a list of the indices at which we want to do the insertions
    inserts = np.repeat(last_row_offsets, max_maz_count - maz_counts)

    # insert zero filler to pad each alternative set to same size
    padded_maz_sizes = np.insert(maz_sizes.size_term.values, inserts, 0.0).reshape(-1, max_maz_count)

    # prob array with one row TAZ_choice, one column per alternative
    row_sums = padded_maz_sizes.sum(axis=1)
    maz_probs = np.divide(padded_maz_sizes, row_sums.reshape(-1, 1))
    assert maz_probs.shape == (num_choosers * taz_sample_size, max_maz_count)

    rands = pipeline.get_rn_generator().random_for_df(chooser_df, n=taz_sample_size)
    rands = rands.reshape(-1, 1)
    assert len(rands) == num_choosers * taz_sample_size
    assert len(rands) == maz_probs.shape[0]

    # make choices
    # positions is array with the chosen alternative represented as a column index in probs
    # which is an integer between zero and max_maz_count
    positions = np.argmax((maz_probs.cumsum(axis=1) - rands) > 0.0, axis=1)

    # shouldn't have chosen any of the dummy pad positions
    assert (positions < maz_counts).all()
    taz_choices[dest_maz_id_col] = maz_sizes[dest_maz_id_col].take(positions + first_row_offsets)
    taz_choices['MAZ_prob'] = maz_probs[np.arange(maz_probs.shape[0]), positions]
    taz_choices['prob'] = taz_choices['TAZ_prob'] * taz_choices['MAZ_prob']

    if have_trace_targets:

        taz_choices_trace_targets = tracing.trace_targets(taz_choices, slicer=CHOOSER_ID)
        trace_taz_choices_df = taz_choices[taz_choices_trace_targets]
        tracing.trace_df(trace_taz_choices_df,
                         label=tracing.extend_trace_label(trace_label, 'taz_choices'),
                         transpose=False)

        lhs_df = trace_taz_choices_df[[CHOOSER_ID, DEST_TAZ]]
        alt_dest_columns = [f'dest_maz_{c}' for c in range(max_maz_count)]

        # following the same logic as the full code, but for trace cutout
        trace_maz_counts = maz_counts[taz_choices_trace_targets]
        trace_last_row_offsets = maz_counts[taz_choices_trace_targets].cumsum()
        trace_inserts = np.repeat(trace_last_row_offsets, max_maz_count - trace_maz_counts)

        # trace dest_maz_alts
        padded_maz_sizes = np.insert(trace_maz_sizes[CHOOSER_ID].values, trace_inserts, 0.0).reshape(-1, max_maz_count)
        df = pd.DataFrame(data=padded_maz_sizes,
                          columns=alt_dest_columns, index=trace_taz_choices_df.index)
        df = pd.concat([lhs_df, df], axis=1)
        tracing.trace_df(df, label=tracing.extend_trace_label(trace_label, 'dest_maz_alts'), transpose=False)

        # trace dest_maz_size_terms
        padded_maz_sizes = np.insert(trace_maz_sizes['size_term'].values, trace_inserts, 0.0).reshape(-1, max_maz_count)
        df = pd.DataFrame(data=padded_maz_sizes,
                          columns=alt_dest_columns, index=trace_taz_choices_df.index)
        df = pd.concat([lhs_df, df], axis=1)
        tracing.trace_df(df, label=tracing.extend_trace_label(trace_label, 'dest_maz_size_terms'), transpose=False)

        # trace dest_maz_probs
        df = pd.DataFrame(data=maz_probs[taz_choices_trace_targets],
                          columns=alt_dest_columns, index=trace_taz_choices_df.index)
        df = pd.concat([lhs_df, df], axis=1)
        df['rand'] = rands[taz_choices_trace_targets]
        tracing.trace_df(df, label=tracing.extend_trace_label(trace_label, 'dest_maz_probs'), transpose=False)

    taz_choices = taz_choices.drop(columns=['TAZ_prob', 'MAZ_prob'])

    # what is the point of this groupby?
    # why aggregate over dest and take the max? shouldn't you either leave it duplicate or sum the prob?
    taz_choices_w_maz = taz_choices.groupby([chooser_id_col, unique_alt_id_col]).agg(
        prob=('prob', 'max'),
        prob_idx=('prob','idxmax'),
        pick_count=('prob', 'count'))
    if unique_alt_id_col != DEST_MAZ:
        taz_choices_w_maz[dest_maz_id_col] = taz_choices.loc[taz_choices_w_maz['prob_idx'], dest_maz_id_col].values
    taz_choices_w_maz = taz_choices_w_maz.drop(columns=['prob_idx'])

    taz_choices_w_maz.reset_index(level=unique_alt_id_col, inplace=True)

    return taz_choices_w_maz


def od_presample(
        spec_segment_name,
        choosers,
        model_settings,
        network_los,
        od_size_terms,
        estimator,
        chunk_size, trace_label,
        orig_maz_src='choosers'):

    trace_label = tracing.extend_trace_label(trace_label, 'presample')

    logger.info(f"{trace_label} od_presample")

    alt_dest_col_name = model_settings['ALT_DEST_COL_NAME']
    alt_od_col_name = model_settings['ALT_OD_COL_NAME_PRESAMPLE']
    orig_maz = model_settings['CHOOSER_ORIG_COL_NAME']
    od_size_term_idx = od_size_terms.index.name
    MAZ_size_terms, TAZ_size_terms = aggregate_size_terms(
        od_size_terms, network_los, dest_maz_id_col=alt_dest_col_name)

    # assert orig_maz in either choosers or size terms
    if ORIG_TAZ not in choosers:
        orig_maz_src = eval(orig_maz_src)
        orig_maz_src[ORIG_TAZ] = map_maz_to_taz(orig_maz_src[orig_maz], network_los)

    # merge agg_dest_size_terms with un-agged MAZ orig terms!!!
    TAZ_size_terms = TAZ_size_terms.reset_index()
    orig_attrs = od_size_terms.drop_duplicates(orig_maz)[model_settings['ORIGIN_ATTR_COLS_TO_USE'] + [orig_maz, ORIG_TAZ]]

    n_repeat = len(orig_attrs)
    TAZ_size_terms = TAZ_size_terms.reindex(TAZ_size_terms.index.repeat(n_repeat))
    TAZ_size_terms[orig_maz] = orig_attrs[orig_maz].tolist() * TAZ_size_terms.index.nunique()

    orig_MAZ_dest_TAZ_size_terms = pd.merge(orig_attrs, TAZ_size_terms, on=orig_maz)
    orig_MAZ_dest_TAZ_size_terms.index.name = alt_od_col_name

    # create wrapper with keys for this lookup - in this case there is a HOME_TAZ in the choosers
    # and a DEST_TAZ in the alternatives which get merged during interaction
    # the skims will be available under the name "skims" for any @ expressions
    skim_dict = network_los.get_skim_dict('taz')
    skims = skim_dict.wrap(ORIG_TAZ, DEST_TAZ)

    orig_maz_dest_taz_sample = _od_sample(
        spec_segment_name,
        choosers,
        orig_MAZ_dest_TAZ_size_terms,
        skims,
        estimator,
        model_settings,
        alt_od_col_name,
        chunk_size,
        trace_label)

    orig_maz_dest_taz_sample[DEST_TAZ] = orig_MAZ_dest_TAZ_size_terms[DEST_TAZ].reindex(
        orig_maz_dest_taz_sample[alt_od_col_name]).values
    # choose a MAZ for each DEST_TAZ choice, choice probability based on
    # MAZ size_term fraction of TAZ total
    maz_choices = choose_MAZ_for_TAZ(
        orig_maz_dest_taz_sample, MAZ_size_terms, trace_label,
        dest_maz_id_col=alt_dest_col_name,
        unique_alt_id_col=alt_od_col_name)
    
    maz_choices[orig_maz] = orig_MAZ_dest_TAZ_size_terms[orig_maz].reindex(maz_choices[alt_od_col_name]).values
    if alt_dest_col_name not in maz_choices:
        assert DEST_MAZ in maz_choices
        maz_choices = maz_choices.rename(columns={DEST_MAZ: alt_dest_col_name})

    # add back original maz-maz od_size_term id
    maz_choices = pd.merge(
        maz_choices.reset_index(),
        od_size_terms[[orig_maz, alt_dest_col_name]].reset_index(),
        on=[orig_maz, alt_dest_col_name])
    maz_choices.set_index(choosers.index.name, inplace=True)
    maz_choices = maz_choices.drop(columns=[alt_od_col_name])

    return maz_choices


class SizeTermCalculator(object):
    """
    convenience object to provide size_terms for a selector (e.g.
    non_mandatory) for various segments (e.g. tour_type or purpose)
    returns size terms for specified segment in df or series form.
    """

    def __init__(self, size_term_selector):

        # do this once so they can request size_terms for various segments (tour_type or purpose)
        land_use = inject.get_table('land_use')
        self.land_use = land_use
        size_terms = inject.get_injectable('size_terms')
        self.destination_size_terms = \
            tour_destination_size_terms(self.land_use, size_terms, size_term_selector)

        assert not self.destination_size_terms.isna().any(axis=None)

    def omnibus_size_terms_df(self):
        return self.destination_size_terms

    def dest_size_terms_df(self, segment_name, trace_label):
        # return size terms as df with one column named 'size_term'
        # convenient if creating or merging with alts

        size_terms = self.destination_size_terms[[segment_name]].copy()
        size_terms.columns = ['size_term']

        # FIXME - no point in considering impossible alternatives (where dest size term is zero)
        logger.debug(f"SizeTermCalculator dropping {(~(size_terms.size_term > 0)).sum()} "
                     f"of {len(size_terms)} rows where size_term is zero for {segment_name}")
        size_terms = size_terms[size_terms.size_term > 0]

        if len(size_terms) == 0:
            logger.warning(f"SizeTermCalculator: no zones with non-zero size terms for {segment_name} in {trace_label}")

        return size_terms

    def od_size_terms_df(
            self, segment_name, trace_label, new_index_name=None, origin_id_col='origin',
            dest_id_col='destination', origin_filter=None, origin_attr_cols=None):
        # if sampling/simulating origins and destinations at the same time,
        # size terms df must be the same shape as the full table of alternatives. by
        # default this will be the n x n set of zone-to-zone pairs. optionally, the
        # user can specify a filter to limit the number of origins or destinations
        # such that the resulting data frame will be n x m elements long where m is 
        # the number of zones left (i.e. the length of the land use table) after
        # applying the filter
        size_terms = self.dest_size_terms_df(segment_name, trace_label)
        land_use = self.land_use.to_frame()
        
        if origin_filter:
            origins = land_use.query(origin_filter)
        else:
            origins = land_use

        n_repeat = len(origins)
        size_terms = size_terms.reindex(size_terms.index.repeat(n_repeat))
        size_terms[origin_id_col] = list(origins.index.values) * size_terms.index.nunique()
        size_terms.index.name = dest_id_col
        size_terms.reset_index(inplace=True)
            
        if new_index_name:
            size_terms.index.name = new_index_name

        # manually add origin attributes to output since these can't be generated by
        # the destination-based size term calculator
        if origin_attr_cols:
            land_use.index.name = origin_id_col
            land_use.reset_index(inplace=True)
            size_terms.reset_index(inplace=True)
            size_terms = pd.merge(
                size_terms, land_use[origin_attr_cols + [origin_id_col]],
                on=origin_id_col, how='left').set_index(new_index_name)

        return size_terms

    def dest_size_terms_series(self, segment_name):
        # return size terms as as series
        # convenient (and no copy overhead) if reindexing and assigning into alts column
        return self.destination_size_terms[segment_name]


def run_od_sample(
        spec_segment_name,
        tours,
        model_settings,
        network_los,
        od_size_terms,
        estimator,
        chunk_size,
        trace_label):

    model_spec = simulate.spec_for_segment(model_settings, spec_id='SAMPLE_SPEC',
                                           segment_name=spec_segment_name, estimator=estimator)

    choosers = tours
    # FIXME - MEMORY HACK - only include columns actually used in spec
    chooser_columns = model_settings['SIMULATE_CHOOSER_COLUMNS']
    choosers = choosers[chooser_columns]

    # interaction_sample requires that choosers.index.is_monotonic_increasing
    if not choosers.index.is_monotonic_increasing:
        logger.debug(f"run_destination_sample {trace_label} sorting choosers because not monotonic_increasing")
        choosers = choosers.sort_index()

    # by default, enable presampling for multizone systems, unless they disable it in settings file
    pre_sample_taz = not (network_los.zone_system == los.ONE_ZONE)
    if pre_sample_taz and not config.setting('want_dest_choice_presampling', True):
        pre_sample_taz = False
        logger.info(f"Disabled destination zone presampling for {trace_label} "
                    f"because 'want_dest_choice_presampling' setting is False")

    # IS IT OK THAT PRESAMPLE CHOICES_DF IS DIFF SIZE THAN SAMPLE CHOICES_DF? IT IS RIGHT?
    if pre_sample_taz:

        logger.info("Running %s destination_presample with %d tours" % (trace_label, len(tours)))

        choices = od_presample(
            spec_segment_name,
            choosers,
            model_settings,
            network_los,
            od_size_terms,
            estimator,
            chunk_size,
            trace_label,
            orig_maz_src='od_size_terms')

    else:
        choices = od_sample(
            spec_segment_name,
            choosers,
            model_settings,
            network_los,
            od_size_terms,
            estimator,
            chunk_size, trace_label)

    origin_col_name = model_settings['ORIG_COL_NAME']
    dest_col_name = model_settings['DEST_COL_NAME']
    alt_od_col_name = model_settings['ALT_OD_COL_NAME']
    if (origin_col_name not in choices.columns) or (dest_col_name not in choices.columns):
        choices = get_od_cols_from_od_id(
            choices, od_size_terms, origin_col_name, dest_col_name, alt_od_col_name)
    choices = choices.sort_index()
    return choices


def run_od_logsums(
        spec_segment_name,
        tours_merged_df,
        od_sample,
        model_settings,
        network_los,
        chunk_size,
        trace_hh_id,
        trace_label):
    """
    add logsum column to existing tour_destination_sample table

    logsum is calculated by running the mode_choice model for each sample (person, dest_zone_id) pair
    in destination_sample, and computing the logsum of all the utilities
    """

    logsum_settings = config.read_model_settings(model_settings['LOGSUM_SETTINGS'])

    # FIXME - MEMORY HACK - only include columns actually used in spec
    tours_merged_df = \
        logsum.filter_chooser_columns(tours_merged_df, logsum_settings, model_settings)

    # merge ods into choosers table
    choosers = od_sample.join(tours_merged_df, how='left')

    logger.info("Running %s with %s rows", trace_label, len(choosers))

    tracing.dump_df(DUMP, choosers, trace_label, 'choosers')

    # run trip mode choice to computer tour mode choice logsums
    if logsum_settings.get('COMPUTE_TRIP_MODE_CHOICE_LOGSUMS', False):

        trip_mode_choice_settings = config.read_model_settings('trip_mode_choice')

        # tours_merged table doesn't yet have all the cols it needs to be called (e.g. 
        # home_zone_id), so in order to compute tour mode choice/trip mode choice logsums
        # in this step we have to pass all tour-level attributes in with the main trips
        # table. see trip_mode_choice.py L56-61 for more details.
        tour_cols_needed = trip_mode_choice_settings.get('TOURS_MERGED_CHOOSER_COLUMNS', [])

        # from tour_mode_choice.py
        not_university = (choosers.tour_type != 'school') | ~choosers.is_university
        choosers['tour_purpose'] = \
            choosers.tour_type.where(not_university, 'univ')

        choosers['stop_frequency'] = '0out_0in'
        choosers['primary_purpose'] = choosers['tour_purpose']
        choosers_og_index = choosers.index.name
        choosers.reset_index(inplace=True)
        choosers.index.name = 'unique_id'
        trips = trip.initialize_from_tours(choosers, ['tour_od_id', 'unique_id'])
        outbound = trips['outbound']
        trips['depart'] = reindex(choosers.start, trips.unique_id)
        trips.loc[~outbound, 'depart'] = reindex(choosers.end, trips.loc[~outbound, 'unique_id'])
        

        logsum_trips = pd.DataFrame()
        nest_spec = config.get_logit_model_settings(logsum_settings)

        # actual coeffs dont matter here, just need them to load the nest structure
        coefficients = simulate.get_segment_coefficients(
            logsum_settings, choosers.iloc[0]['tour_purpose'])
        nest_spec = simulate.eval_nest_coefficients(nest_spec, coefficients, trace_label)
        tour_mode_alts = []
        for nest in logit.each_nest(nest_spec):
            if nest.is_leaf:
                tour_mode_alts.append(nest.name)

        # repeat rows from the trips table iterating over tour mode
        for tour_mode in tour_mode_alts:
            trips['tour_mode'] = tour_mode
            logsum_trips = pd.concat((logsum_trips, trips), ignore_index=True)
        assert len(logsum_trips) == len(trips) * len(tour_mode_alts)
        logsum_trips.index.name = 'trip_id'

        for col in tour_cols_needed:
            if col not in trips:
                logsum_trips[col] = reindex(choosers[col], logsum_trips.unique_id)

        pipeline.replace_table('trips', logsum_trips)
        tracing.register_traceable_table('trips', logsum_trips)
        pipeline.get_rn_generator().add_channel('trips', logsum_trips)

        choosers.set_index(choosers_og_index, drop=True, inplace=True)

        # run trip mode choice on pseudo-trips. use orca instead of pipeline to
        # execute the step because pipeline can only handle one open step at a time
        orca.run(['trip_mode_choice'])

        # grab trip mode choice logsums and pivot by tour mode and direction, index
        # on tour_id to enable merge back to choosers table
        trips = inject.get_table('trips').to_frame()
        trip_dir_mode_logsums = trips.pivot(
            index=['tour_id', 'tour_od_id'], columns=['tour_mode', 'outbound'], values='trip_mode_choice_logsum')
        new_cols = [
            '_'.join(['logsum', mode, 'outbound' if outbound else 'inbound'])
            for mode, outbound in trip_dir_mode_logsums.columns]
        trip_dir_mode_logsums.columns = new_cols
        choosers.reset_index(inplace=True)
        choosers.set_index(['tour_id', 'tour_od_id'], inplace=True)
        choosers = pd.merge(choosers, trip_dir_mode_logsums, left_index=True, right_index=True)
        choosers.reset_index(inplace=True)
        choosers.set_index(choosers_og_index, inplace=True)
        pipeline.get_rn_generator().drop_channel('trips')

    logsums = logsum.compute_logsums(
        choosers,
        spec_segment_name,
        logsum_settings,
        model_settings,
        network_los,
        chunk_size,
        trace_label, 'start', 'end', 'duration')

    od_sample['tour_mode_choice_logsum'] = logsums

    return od_sample


def run_od_simulate(
        spec_segment_name,
        tours,
        od_sample,
        model_settings,
        network_los,
        od_size_terms,
        estimator,
        chunk_size,
        trace_label):
    """
    run destination_simulate on tour_destination_sample
    annotated with mode_choice logsum to select a destination from sample alternatives
    """

    model_spec = simulate.spec_for_segment(model_settings, spec_id='SPEC',
                                           segment_name=spec_segment_name, estimator=estimator)

    # merge persons into tours
    choosers = tours

    # FIXME - MEMORY HACK - only include columns actually used in spec
    chooser_columns = model_settings['SIMULATE_CHOOSER_COLUMNS']
    choosers = choosers[chooser_columns]
    if estimator:
        estimator.write_choosers(choosers)

    alt_od_col_name = model_settings['ALT_OD_COL_NAME']
    origin_col_name = model_settings['ORIG_COL_NAME']
    dest_col_name = model_settings['DEST_COL_NAME']

    # alternatives are pre-sampled and annotated with logsums and pick_count
    # but we have to merge size_terms column into alt sample list
    if od_size_terms.index.name in od_size_terms.columns:
        del od_size_terms[od_size_terms.index.name]
    od_sample = od_sample.merge(
        od_size_terms, how='left', left_on='tour_od_id', right_index=True, suffixes=('','_st'))
    if origin_col_name + '_st' in od_sample.columns:
        if all(od_sample[origin_col_name + '_st'] == od_sample[origin_col_name]):
            del od_sample[origin_col_name + '_st']
        else:
            logger.error('origin column values od_sample df need reconciling')
        if all(od_sample[dest_col_name + '_st'] == od_sample[dest_col_name]):
            del od_sample[dest_col_name + '_st']
        else:
            logger.error('destination column values in od_sample df need reconciling')

    tracing.dump_df(DUMP, od_sample, trace_label, 'alternatives')

    constants = config.get_model_constants(model_settings)

    logger.info("Running tour_destination_simulate with %d persons", len(choosers))

    # create wrapper with keys for this lookup - in this case there is a home_zone_id in the choosers
    # and a zone_id in the alternatives which get merged during interaction
    # the skims will be available under the name "skims" for any @ expressions
    skim_dict = network_los.get_default_skim_dict()
    skims = skim_dict.wrap(origin_col_name, dest_col_name)

    locals_d = {
        'skims': skims,
    }
    if constants is not None:
        locals_d.update(constants)

    tracing.dump_df(DUMP, choosers, trace_label, 'choosers')

    choices = interaction_sample_simulate(
        choosers,
        od_sample,
        spec=model_spec,
        choice_column=alt_od_col_name,
        skims=skims,
        locals_d=locals_d,
        chunk_size=chunk_size,
        trace_label=trace_label,
        trace_choice_name='destination',
        estimator=estimator)

    choices = choices.to_frame('choice')

    return choices


def get_od_cols_from_od_id(choices_df, reference_df, orig_col_name, dest_col_name, choice_col='choice'):
    
    choices_df[orig_col_name] = reference_df.reindex(choices_df[choice_col])[orig_col_name].values
    choices_df[dest_col_name] = reference_df.reindex(choices_df[choice_col])[dest_col_name].values

    return choices_df


def run_tour_od(
        tours,
        persons,
        want_sample_table,
        model_settings,
        network_los,
        estimator,
        chunk_size, trace_hh_id, trace_label):

    size_term_calculator = SizeTermCalculator(model_settings['SIZE_TERM_SELECTOR'])
    size_term_index_name = model_settings['ALT_OD_COL_NAME']
    origin_col_name = model_settings['ORIG_COL_NAME']
    dest_col_name = model_settings['DEST_COL_NAME']
    
    origin_filter = None
    if model_settings['ORIG_FILTER']:
        origin_filter = model_settings['ORIG_FILTER']

    chooser_segment_column = model_settings['CHOOSER_SEGMENT_COLUMN_NAME']

    # maps segment names to compact (integer) ids
    segments = model_settings['SEGMENTS']

    # interaction_sample_simulate insists choosers appear in same order as alts
    tours = tours.sort_index()

    choices_list = []
    sample_list = []
    for segment_name in segments:

        choosers = tours[tours[chooser_segment_column] == segment_name]

        choosers = pd.merge(
            choosers, persons.to_frame(columns=['is_university', 'demographic_segment']),
            left_on='person_id', right_index=True)

        # size_term segment is segment_name
        origin_attr_cols = model_settings['ORIGIN_ATTR_COLS_TO_USE']
        dest_size_terms_df = size_term_calculator.dest_size_terms_df(segment_name, trace_label)
        segment_od_size_terms = size_term_calculator.od_size_terms_df(
            segment_name, trace_label, size_term_index_name, origin_col_name, dest_col_name,
            origin_filter, origin_attr_cols)

        if choosers.shape[0] == 0:
            logger.info("%s skipping segment %s: no choosers", trace_label, segment_name)
            continue

        # - od_sample
        spec_segment_name = segment_name  # spec_segment_name is segment_name

        od_sample_df = \
            run_od_sample(
                spec_segment_name,
                choosers,
                model_settings,
                network_los,
                segment_od_size_terms,
                estimator,
                chunk_size=chunk_size,
                trace_label=tracing.extend_trace_label(trace_label, 'sample.%s' % segment_name))

        # - destination_logsums
        od_sample_df = \
            run_od_logsums(
                spec_segment_name,
                choosers,
                od_sample_df,
                model_settings,
                network_los,
                chunk_size=chunk_size,
                trace_hh_id=trace_hh_id,
                trace_label=tracing.extend_trace_label(trace_label, 'logsums.%s' % segment_name))

        # - od_simulate
        choices = \
            run_od_simulate(
                spec_segment_name,
                choosers,
                od_sample=od_sample_df,
                model_settings=model_settings,
                network_los=network_los,
                od_size_terms=segment_od_size_terms,
                estimator=estimator,
                chunk_size=chunk_size,
                trace_label=tracing.extend_trace_label(trace_label, 'simulate.%s' % segment_name))

        choices[origin_col_name] = segment_od_size_terms.reindex(choices.choice)[origin_col_name].values
        choices[dest_col_name] = segment_od_size_terms.reindex(choices.choice)[dest_col_name].values

        choices_list.append(choices)

        if want_sample_table:
            # FIXME - sample_table
            od_sample_df.set_index(model_settings['ALT_DEST_COL_NAME'],
                                         append=True, inplace=True)
            sample_list.append(location_sample_df)

        # FIXME - want to do this here?
        del od_sample_df
        force_garbage_collect()

    if len(choices_list) > 0:
        choices_df = pd.concat(choices_list)

    if len(sample_list) > 0:
        save_sample_df = pd.concat(sample_list)
    else:
        # this could happen either with small samples as above, or if no saved sample desired
        save_sample_df = None

    return choices_df, save_sample_df
