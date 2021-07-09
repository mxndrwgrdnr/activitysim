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
from activitysim.core import expressions

# from .util import estimation

logger = logging.getLogger(__name__)


def resample_school_zones(choosers, land_use, model_settings, col_to_override='school_zone_id'):
    """
    Re-samples the university school zone based only on enrollment. Can apply to the original school
    zone id or subsequent university trips.

    Parameters
    ----------
    choosers : pd.DataFrame
        subset of persons or trips that will have their school zone re-sampled
    land_use: pd.DataFrame
        land_use data containing columns specifying university coding and enrollment
    model_settings: dict
        parameters in university_location_zone_override.yaml
    col_to_override:
        choosers column to set new sampled zone_id to: school_zone_id or (trip) destination

    Returns
    ----------
    choosers: pd.DataFrame
        with new university zone id set and original zone id stored if specified in config
    """
    original_zone_col_name = model_settings['ORIGINAL_ZONE_COL_NAME']
    univ_enrollment_col_name = model_settings['LANDUSE_UNIV_ENROL_COL_NAME']
    landuse_univ_code_col_name = model_settings['LANDUSE_UNIV_CODE_COL_NAME']
    allowed_univ_codes = model_settings['UNIV_CODES_TO_OVERRIDE']
    random_state = model_settings['RANDOM_STATE']

    if original_zone_col_name is not None:
        choosers[original_zone_col_name] = pd.NA

    # Override school_zone_id for each requested university separately
    for univ_code in allowed_univ_codes:
        # selecting land use data
        univ_land_use = land_use[land_use[landuse_univ_code_col_name] == univ_code].reset_index()

        if len(univ_land_use) == 0:
            logger.info("No zones found for university code: %s", univ_code)
            continue

        # selecting only university students with school_zone_id matching university code
        choosers_to_override = (choosers[col_to_override].isin(univ_land_use.zone_id))

        num_choosers_to_override = choosers_to_override.sum()
        logger.info("Re-sampling %s zones for university with code: %s",
                    num_choosers_to_override, univ_code)

        if original_zone_col_name is not None:
            choosers.loc[choosers_to_override,
                         original_zone_col_name] = choosers.loc[choosers_to_override, col_to_override]

        # override school id based on university enrollment alone
        choosers.loc[choosers_to_override, col_to_override] = univ_land_use.zone_id.sample(
            n=num_choosers_to_override,
            weights=univ_land_use[univ_enrollment_col_name],
            replace=True,
            random_state=random_state).to_numpy()

    return choosers


@inject.step()
def university_location_zone_override(
        persons_merged, persons, land_use,
        chunk_size, trace_hh_id):
    """
    This model overrides the school taz for students attending large universities.  New school tazs
    are chosen based on the university enrollment in landuse without accessibility terms.  This is
    done to replicate the fact that university students can have classes all over campus.

    The main interface to this model is the university_location_zone_override() function.
    This function is registered as an orca step in the example Pipeline.
    """

    trace_label = 'university_location_zone_override'
    model_settings_file_name = 'university_location_zone_override.yaml'

    choosers = persons.to_frame()
    land_use_df = land_use.to_frame()

    univ_school_seg = config.read_model_settings('constants.yaml')['SCHOOL_SEGMENT_UNIV']
    choosers = choosers[
        (choosers.school_zone_id > -1) & (choosers.school_segment == univ_school_seg)]

    logger.info("Running %s for %d university students", trace_label, len(choosers))

    model_settings = config.read_model_settings(model_settings_file_name)

    choosers = resample_school_zones(
        choosers, land_use_df, model_settings, col_to_override='school_zone_id')

    # Overriding school_zone_id in persons table
    persons = persons.to_frame()
    persons.loc[persons.index.isin(choosers.index),
                'school_zone_id'] = choosers['school_zone_id'].astype(int)

    # saving original zone if desired
    original_zone_col_name = model_settings['ORIGINAL_ZONE_COL_NAME']
    if original_zone_col_name is not None:
        persons.loc[persons.index.isin(choosers.index),
                    original_zone_col_name] = choosers[original_zone_col_name]

    pipeline.replace_table("persons", persons)

    tracing.print_summary('university_location_zone_override choices',
                          persons['school_zone_id'],
                          value_counts=True)

    if trace_hh_id:
        tracing.trace_df(persons,
                         label=trace_label,
                         warn_if_empty=True)


@inject.step()
def trip_destination_univ_zone_override(
        trips, tours, land_use,
        chunk_size, trace_hh_id):
    """
    This model overrides the university trip destination zone for students attending large universities.
    New school tazs are chosen based on the university enrollment in landuse without accessibility terms.
    This is done to replicate the fact that university students can have classes all over campus.
    If the trip destination is the primary tour destination, the zone is not changed because it was
    already handled in university_location_zone_override.

    The main interface to this model is the trip_destination_univ_zone_override() function.
    This function is registered as an orca step in the example Pipeline.
    """

    trace_label = 'trip_destination_univ_zone_override'
    model_settings_file_name = 'university_location_zone_override.yaml'
    model_settings = config.read_model_settings(model_settings_file_name)
    univ_purpose = model_settings['TRIP_UNIVERSITY_PURPOSE']
    tour_mode_override_dict = model_settings['TOUR_MODE_OVERRIDE_DICT']

    choosers = trips.to_frame()
    land_use_df = land_use.to_frame()
    tours = tours.to_frame()

    # primary trips are outbound trips where the next trip is not outbound
    choosers['is_primary_trip'] = np.where(
        (choosers['outbound']) & ~(choosers['outbound'].shift(-1)),
        True, False)
    print(choosers['is_primary_trip'].value_counts())
    choosers = choosers[~(choosers['is_primary_trip']) & (choosers['purpose'] == univ_purpose)]

    # changing tour mode according to model settings to avoid, e.g. really long walk trips
    # This has to be done here and not in university_location_zone_override because
    # this model comes after tour mode choice
    if tour_mode_override_dict is not None:
        tours_with_trip_resampled = choosers['tour_id']
        for orig_tour_mode, new_tour_mode in tour_mode_override_dict.items():
            tour_overrides = ((tours.index.isin(choosers.tour_id))
                              & (tours['tour_mode'] == orig_tour_mode))
            logger.info("Changing %d tours with mode %s to mode %s",
                        tour_overrides.sum(), orig_tour_mode, new_tour_mode)
            tours.loc[tour_overrides, 'tour_mode'] = new_tour_mode

    logger.info("Running %s for %d university students", trace_label, len(choosers))

    choosers = resample_school_zones(
        choosers, land_use_df, model_settings, col_to_override='destination')

    # Overriding school_zone_id in persons table
    trips = trips.to_frame()
    trips.loc[trips.index.isin(choosers.index), 'destination'] = choosers['destination'].astype(int)

    # need to change subsequent origin for trips that were changed
    trips['last_destination'] = trips.groupby('tour_id')['destination'].transform('shift')
    trips['origin'] = np.where(
        trips['last_destination'].notna() & (trips['last_destination'] != trips['origin']),
        trips['last_destination'],
        trips['origin']
        )
    trips.drop(columns='last_destination', inplace=True)

    # saving old zone choice if requested
    original_zone_col_name = model_settings['ORIGINAL_ZONE_COL_NAME']
    if original_zone_col_name is not None:
        trips.loc[trips.index.isin(choosers.index),
                  original_zone_col_name] = choosers[original_zone_col_name]

    pipeline.replace_table("trips", trips)
    pipeline.replace_table("tours", tours)

    tracing.print_summary('trip_destination_univ_zone_override for zones',
                          trips[original_zone_col_name],
                          value_counts=True)

    if trace_hh_id:
        tracing.trace_df(trips,
                         label=trace_label,
                         warn_if_empty=True)
