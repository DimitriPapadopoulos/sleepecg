# © SleepECG developers
#
# License: BSD (3-clause)

"""Read datasets containing ECG data and sleep stage annotations."""

from __future__ import annotations

import csv
import datetime
import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import NamedTuple
from xml.etree import ElementTree

import numpy as np

from sleepecg.config import get_config_value
from sleepecg.heartbeats import detect_heartbeats
from sleepecg.io.nsrr import _download_nsrr_file, _get_nsrr_url, _list_nsrr, download_nsrr
from sleepecg.io.physionet import _list_physionet, download_physionet


class SleepStage(IntEnum):
    """
    Mapping of AASM sleep stages to integers.

    To facilitate hypnogram plotting, values start with zero and increase with wakefulness.
    """

    UNDEFINED = 0
    N3 = 1
    N2 = 2
    N1 = 3
    REM = 4
    WAKE = 5


class Gender(IntEnum):
    """Mapping of gender to integers."""

    FEMALE = 0
    MALE = 1


@dataclass
class SubjectData:
    """
    Store data about a single subject.

    Attributes
    ----------
    gender : int, optional
        The subject's gender, stored as an integer as defined by `Gender`, by default
        `None`.
    age : int, optional
        The subject's age in years, by default `None`.
    weight : float, optional
        The subject's weight in kg, by default `None`.
    """

    gender: int | None = None
    age: int | None = None
    weight: float | None = None


@dataclass
class SleepRecord:
    """
    Store a single sleep record.

    Attributes
    ----------
    sleep_stages : np.ndarray, optional
        Sleep stages according to AASM guidelines, stored as integers as defined by
        `SleepStage`, by default `None`.
    sleep_stage_duration : int, optional
        Duration of each sleep stage in seconds, by default `None`.
    id : str, optional
        The record ID, by default `None`.
    recording_start_time : datetime.time, optional
        Time at which the recording was started, by default `None`.
    heartbeat_times : np.ndarray, optional
        Times of heartbeats relative to recording start in seconds, by default `None`.
    subject_data : SubjectData, optional
        Dataclass containing subject data (such as gender or age), by default `None`.
    activity_counts: np.ndarray, optional
        Activity counts according to Actiwatch actigraphy, by default `None`.
    """

    sleep_stages: np.ndarray | None = None
    sleep_stage_duration: int | None = None
    id: str | None = None
    recording_start_time: datetime.time | None = None
    heartbeat_times: np.ndarray | None = None
    subject_data: SubjectData | None = None
    activity_counts: np.ndarray | None = None


class _ParseNsrrXmlResult(NamedTuple):
    sleep_stages: np.ndarray
    sleep_stage_duration: int
    recording_start_time: datetime.time
    recording_duration: float


def _parse_nsrr_xml(xml_filepath: Path) -> _ParseNsrrXmlResult:
    """
    Parse NSRR XML sleep stage annotation file.

    Parameters
    ----------
    xml_filepath : pathlib.Path
        Path of the annotation file to read.

    Returns
    -------
    sleep_stages : np.ndarray
        Sleep stages according to AASM guidelines, stored as integers as defined by
        `SleepStage`.
    sleep_stage_duration : int
        Duration of each sleep stage in seconds.
    recording_start_time : datetime.time
        Time at which the recording was started.
    recording_duration: float
        Duration of the recording in seconds.

    """
    STAGE_MAPPING = {
        "Wake|0": SleepStage.WAKE,
        "Stage 1 sleep|1": SleepStage.N1,
        "Stage 2 sleep|2": SleepStage.N2,
        "Stage 3 sleep|3": SleepStage.N3,
        "Stage 4 sleep|4": SleepStage.N3,
        "REM sleep|5": SleepStage.REM,
        "Unscored|9": SleepStage.UNDEFINED,
    }

    root = ElementTree.parse(xml_filepath).getroot()

    if (epoch_length_str := root.findtext("EpochLength")) is None:
        raise RuntimeError(f"EpochLength not found in {xml_filepath}.")
    epoch_length = int(epoch_length_str)

    if (scored_event := root.find(".//ScoredEvent")) is None:
        raise RuntimeError(f"Recording duration not found in {xml_filepath}.")
    recording_duration = float(scored_event.findtext(".//Duration", ""))

    start_time = None
    annot_stages = []

    for event in root.findall("ScoredEvents")[0]:
        if event.findtext("EventConcept") == "Recording Start Time":
            start_time = datetime.datetime.strptime(
                event.findtext("ClockTime", "").split()[1], "%H.%M.%S"
            ).time()

        if event.findtext("EventType") == "Stages|Stages":
            epoch_duration = int(float(event.findtext("Duration", "")))
            stage = STAGE_MAPPING[event.findtext("EventConcept", "")]
            annot_stages.extend([stage] * int(epoch_duration / epoch_length))

    if start_time is None:
        raise RuntimeError(f"'Recording Start Time' not found in {xml_filepath}.")

    return _ParseNsrrXmlResult(
        np.array(annot_stages, dtype=np.int8), epoch_length, start_time, recording_duration
    )


def read_mesa(
    records_pattern: str = "*",
    heartbeats_source: str = "annotation",
    offline: bool = False,
    keep_edfs: bool = False,
    data_dir: str | Path | None = None,
    activity_source: str | None = None,
) -> Iterator[SleepRecord]:
    """
    Lazily read records from [MESA](https://sleepdata.org/datasets/mesa).

    Each MESA record consists of an `.edf` file containing raw polysomnography data and an
    `.xml` file containing annotated events. Since the entire MESA dataset requires about
    385 GB of disk space, `.edf` files can be deleted after heartbeat times have been
    extracted. Heartbeat times are cached in an `.npy` file in
    `<data_dir>/mesa/preprocessed/heartbeats`.

    Parameters
    ----------
    records_pattern : str, optional
         Glob-like pattern to select record IDs, by default `'*'`.
    heartbeats_source : {'annotation', 'cached', 'ecg'}, optional
        If `'annotation'` (default), get heartbeat times from
        `polysomnography/annotations-rpoints/<record_id>-rpoints.csv` (not available for all
        records). If `'ecg'`, use `sleepecg.detect_heartbeats()` on the ECG contained in
        `polysomnography/edfs/<record_id>.edf` and cache the result in
        `preprocessed/heartbeats/<record_id>.npy`. If `'cached'`, get the cached heartbeats.
    offline : bool, optional
        If `True`, search for local files only instead of using the NSRR API, by default
        `False`.
    keep_edfs : bool, optional
        If `False`, remove `.edf` after heartbeat detection, by default `False`.
    data_dir : str | pathlib.Path, optional
        Directory where all datasets are stored. If `None` (default), the value will be
        taken from the configuration.
    activity_source : {'actigraphy', 'cached', None}, optional
        If `None` (default), actigraphy data will not be downloaded. If `'actigraphy'`,
        download actigraphy data from MESA dataset. If `'cached'`, get the cached activity
        counts.

    Yields
    ------
    SleepRecord
        Each element in the generator is of type `SleepRecord`.
    """
    from edfio import read_edf

    DB_SLUG = "mesa"
    ANNOTATION_DIRNAME = "polysomnography/annotations-events-nsrr"
    EDF_DIRNAME = "polysomnography/edfs"
    ACTIVITY_DIRNAME = "actigraphy"
    OVERLAP_DIRNAME = "overlap"
    HEARTBEATS_DIRNAME = "preprocessed/heartbeats"
    ACTIVITY_COUNTS_DIRNAME = "preprocessed/activity_counts"
    RPOINTS_DIRNAME = "polysomnography/annotations-rpoints"

    GENDER_MAPPING = {0: Gender.FEMALE, 1: Gender.MALE}

    heartbeats_source_options = {"annotation", "cached", "ecg"}
    if heartbeats_source not in heartbeats_source_options:
        raise ValueError(
            f"Invalid value for parameter `heartbeats_source`: {heartbeats_source}, "
            f"possible options: {heartbeats_source_options}"
        )

    activity_source_options = {None, "cached", "actigraphy"}
    if activity_source not in activity_source_options:
        raise ValueError(
            f"Invalid value for parameter `activity_source`: {activity_source}, "
            f"possible options: {activity_source_options}"
        )

    if data_dir is None:
        data_dir = get_config_value("data_dir")

    db_dir = Path(data_dir).expanduser() / DB_SLUG
    annotations_dir = db_dir / ANNOTATION_DIRNAME
    edf_dir = db_dir / EDF_DIRNAME
    heartbeats_dir = db_dir / HEARTBEATS_DIRNAME

    for directory in (annotations_dir, edf_dir, heartbeats_dir):
        directory.mkdir(parents=True, exist_ok=True)

    if activity_source is not None:
        activity_dir = db_dir / ACTIVITY_DIRNAME
        activity_counts_dir = db_dir / ACTIVITY_COUNTS_DIRNAME
        overlap_dir = db_dir / OVERLAP_DIRNAME
        for directory in (activity_dir, activity_counts_dir, overlap_dir):
            directory.mkdir(parents=True, exist_ok=True)

    if not offline:
        download_url = _get_nsrr_url(DB_SLUG)

        subject_data_filename, subject_data_checksum = _list_nsrr(
            "mesa",
            "datasets",
            "mesa-sleep-dataset-*.csv",
            shallow=True,
        )[0]
        subject_data_filepath = db_dir / subject_data_filename
        _download_nsrr_file(
            download_url + subject_data_filename,
            target_filepath=subject_data_filepath,
            checksum=subject_data_checksum,
        )

        xml_files = _list_nsrr(
            DB_SLUG,
            ANNOTATION_DIRNAME,
            f"mesa-sleep-{records_pattern}-nsrr.xml",
            shallow=True,
        )
        checksums = dict(xml_files)
        requested_records = [Path(file).stem[:-5] for file, _ in xml_files]

        edf_files = _list_nsrr(
            DB_SLUG,
            EDF_DIRNAME,
            f"mesa-sleep-{records_pattern}.edf",
            shallow=True,
        )
        checksums.update(edf_files)

        rpoints_files = _list_nsrr(
            DB_SLUG,
            RPOINTS_DIRNAME,
            f"mesa-sleep-{records_pattern}-rpoint.csv",
            shallow=True,
        )
        checksums.update(rpoints_files)

        if activity_source is not None:
            activity_files = _list_nsrr(
                db_slug=DB_SLUG,
                subfolder="actigraphy",
                pattern=f"mesa-sleep-{records_pattern}.csv",
                shallow=True,
            )
            checksums.update(activity_files)
            overlap_filename, overlap_checksum = _list_nsrr(
                db_slug="mesa", subfolder="overlap", shallow=True
            )[0]
            overlap_filepath = db_dir / overlap_filename
            _download_nsrr_file(
                download_url + overlap_filename,
                target_filepath=overlap_filepath,
                checksum=overlap_checksum,
            )

    else:
        subject_data_filepath = next((db_dir / "datasets").glob("mesa-sleep-dataset-*.csv"))
        xml_paths = annotations_dir.glob(f"mesa-sleep-{records_pattern}-nsrr.xml")
        requested_records = sorted([file.stem[:-5] for file in xml_paths])
        if activity_source == "actigraphy":
            overlap_filename = "mesa-actigraphy-psg-overlap.csv"
            overlap_filepath = overlap_dir / overlap_filename
            if not overlap_filepath.is_file():
                raise RuntimeError(
                    "Overlap file not found, make sure it is in the correct directory "
                    "overlap/mesa-actigraphy-psg-overlap.csv."
                )

    subject_data_array = np.loadtxt(
        subject_data_filepath,
        delimiter=",",
        skiprows=1,
        usecols=[0, 3, 5],  # [mesaid, gender, age]
        dtype=int,
    )

    subject_data = {}
    for mesaid, gender, age in subject_data_array:
        subject_data[f"mesa-sleep-{mesaid:04}"] = SubjectData(
            gender=GENDER_MAPPING[gender],
            age=age,
        )

    if activity_source == "actigraphy":
        overlap_data = {}

        with open(overlap_filepath) as csv_file:
            overlap_reader = csv.DictReader(csv_file, delimiter=",")
            for entry in overlap_reader:
                mesaid = int(entry["mesaid"])
                line = int(entry["line"])
                overlap_data[mesaid] = line

    for record_id in requested_records:
        heartbeats_file = heartbeats_dir / f"{record_id}.npy"
        if heartbeats_source == "annotation":
            rpoints_filename = f"{RPOINTS_DIRNAME}/{record_id}-rpoint.csv"
            rpoints_filepath = db_dir / rpoints_filename
            if not rpoints_filepath.is_file():
                if not offline and rpoints_filename in checksums:
                    _download_nsrr_file(
                        download_url + rpoints_filename,
                        rpoints_filepath,
                        checksums[rpoints_filename],
                    )
                else:
                    print(f"Skipping {record_id} due to missing heartbeat annotations.")
                    continue

            heartbeat_times = np.loadtxt(
                rpoints_filepath,
                delimiter=",",
                skiprows=1,
                usecols=18,  # column 18 ('seconds') contains the annotated heartbeat times
            )
            # for some reason some (39) records have unsorted annotations
            heartbeat_times.sort()
        elif heartbeats_source == "cached":
            if not heartbeats_file.is_file():
                print(f"Skipping {record_id} due to missing cached heartbeats.")
                continue
            heartbeat_times = np.load(heartbeats_file)
        elif heartbeats_source == "ecg":
            edf_filename = EDF_DIRNAME + f"/{record_id}.edf"
            edf_filepath = db_dir / edf_filename
            edf_was_available = edf_filepath.is_file()
            if not offline:
                _download_nsrr_file(
                    download_url + edf_filename,
                    edf_filepath,
                    checksums[edf_filename],
                )

            ecg = read_edf(edf_filepath).get_signal("EKG")
            heartbeat_indices = detect_heartbeats(ecg.data, ecg.sampling_frequency)
            heartbeat_times = heartbeat_indices / ecg.sampling_frequency
            np.save(heartbeats_file, heartbeat_times)

            if not edf_was_available and not keep_edfs:
                edf_filepath.unlink()

        xml_filename = ANNOTATION_DIRNAME + f"/{record_id}-nsrr.xml"
        xml_filepath = db_dir / xml_filename
        if not offline:
            _download_nsrr_file(
                download_url + xml_filename,
                xml_filepath,
                checksums[xml_filename],
            )

        parsed_xml = _parse_nsrr_xml(xml_filepath)

        activity_counts = None
        if activity_source is not None:
            activity_counts_file = activity_counts_dir / f"{record_id}-activity-counts.npy"
            if activity_source == "cached":
                if not activity_counts_file.is_file():
                    print(f"Skipping {record_id} due to missing cached activity counts.")
                    continue
                activity_counts = np.load(activity_counts_file)
            else:
                mesaid = int(record_id.split("-")[2])
                if mesaid not in overlap_data:
                    print(f"Skipping {record_id} due to missing overlap data.")
                    continue

                activity_filename = ACTIVITY_DIRNAME + f"/{record_id}.csv"
                activity_filepath = db_dir / activity_filename
                if not offline:
                    _download_nsrr_file(
                        download_url + activity_filename,
                        activity_filepath,
                        checksums[activity_filename],
                    )

                if not os.path.exists(activity_filepath):
                    print(f"Skipping {record_id} due to missing activity data.")
                    continue

                with open(activity_filepath) as csv_file:
                    reader = csv.reader(csv_file, delimiter=",")
                    header = next(reader)
                    activity_data = [dict(zip(header, row)) for row in reader]

                recording_start_time = parsed_xml.recording_start_time
                recording_duration = parsed_xml.recording_duration
                recording_end_time = datetime.datetime.combine(
                    datetime.datetime.today(), recording_start_time
                ) + datetime.timedelta(seconds=recording_duration)

                recording_end_time_seconds = recording_end_time.second
                rounding_seconds = (
                    (30 - recording_end_time_seconds % 30)
                    if recording_end_time_seconds % 30 >= 15
                    else -(recording_end_time_seconds % 30)
                )

                recording_end_time = recording_end_time + datetime.timedelta(
                    seconds=rounding_seconds
                )
                recording_end_time_str = recording_end_time.strftime("%H:%M:%S").lstrip("0")

                start_line = overlap_data[mesaid] + 1

                for item in activity_data:
                    if item.get("linetime") == recording_end_time_str:
                        end_line = int(item["line"])
                        break
                else:
                    print(
                        f"Skipping {record_id} due to missing line matching "
                        f"{recording_end_time_str}."
                    )
                    continue

                activity_counts = [
                    item["activity"] for item in activity_data[start_line:end_line]
                ]

                activity_counts = np.array(activity_counts)

                diff = len(activity_counts) - len(parsed_xml.sleep_stages)
                if abs(diff) > 2:
                    print(f"Skipping {record_id} due to invalid activity counts.")
                    continue
                elif diff > 0:
                    activity_counts = activity_counts[:-diff]
                elif diff < 0:
                    activity_counts = np.append(activity_counts, activity_counts[diff:])

                activity_counts[activity_counts == ""] = "0"
                activity_counts = activity_counts.astype(float)
                np.save(activity_counts_file, activity_counts)

        yield SleepRecord(
            sleep_stages=parsed_xml.sleep_stages,
            sleep_stage_duration=parsed_xml.sleep_stage_duration,
            id=record_id,
            recording_start_time=parsed_xml.recording_start_time,
            heartbeat_times=heartbeat_times,
            subject_data=subject_data[record_id],
            activity_counts=activity_counts,
        )


def read_slpdb(
    records_pattern: str = "*",
    offline: bool = False,
    data_dir: str | Path | None = None,
) -> Iterator[SleepRecord]:
    """
    Lazily read records from [SLPDB](https://physionet.org/content/slpdb).

    Required files are downloaded from PhysioNet to `<data_dir>/slpdb`.

    Parameters
    ----------
    records_pattern : str, optional
         Glob-like pattern to select record IDs, by default `'*'`.
    offline : bool, optional
        If `True`, search for local files only instead of downloading from PhysioNet, by
        default `False`.
    data_dir : str | pathlib.Path, optional
        Directory where all datasets are stored. If `None` (default), the value will be
        taken from the configuration.

    Yields
    ------
    SleepRecord
        Each element in the generator is of type `SleepRecord`.
    """
    # https://physionet.org/content/slpdb/1.0.0/
    import wfdb

    DB_SLUG = "slpdb"

    STAGE_MAPPING = {
        "W": SleepStage.WAKE,
        "R": SleepStage.REM,
        "1": SleepStage.N1,
        "2": SleepStage.N2,
        "3": SleepStage.N3,
        "4": SleepStage.N3,
    }

    if data_dir is None:
        data_dir = get_config_value("data_dir")

    data_dir = Path(data_dir).expanduser()
    db_dir = data_dir / DB_SLUG

    requested_records = _list_physionet(
        data_dir=data_dir,
        db_slug=DB_SLUG,
        pattern=records_pattern,
    )

    if not offline:
        download_physionet(
            db_slug=DB_SLUG,
            requested_records=requested_records,
            extensions=[".hea", ".dat", ".st"],
            data_dir=data_dir,
        )

    for record_id in requested_records:
        record_file = str(db_dir / record_id)

        record = wfdb.rdrecord(record_file)
        start_time = record.base_time
        ecg = np.asarray(record.p_signal[:, record.sig_name.index("ECG")])
        fs = record.fs

        heartbeat_indices = detect_heartbeats(ecg, fs)
        heartbeat_times = heartbeat_indices / fs

        annot_st = wfdb.rdann(record_file, "st")

        # Some 30 second windows don't have a sleep stage annotation, so the annotation
        # array is initialized with `SleepStage.UNDEFINED` for every 30 second window.
        for sample_time, annotation in zip(annot_st.sample[::-1], annot_st.aux_note[::-1]):
            if annotation[0] in STAGE_MAPPING:
                number_of_sleep_stages = sample_time // (30 * fs) + 1
                break

        sleep_stages = np.full(number_of_sleep_stages, SleepStage.UNDEFINED)

        # Most annotations are at sample indices which are multiples of 30*fs. However,
        # annotations which would be at sample index 0, are at sample index 1. Integer
        # division is used when calculating the stage index to move these annotations to
        # sample index 0.
        for sample_time, annotation in zip(annot_st.sample, annot_st.aux_note):
            if annotation[0] in STAGE_MAPPING:
                sleep_stages[sample_time // (30 * fs)] = STAGE_MAPPING[annotation[0]]

        # Age and weight are given in the last line of the header file, which is contained
        # in record.comments[0] and looks like this:
        # '44 M 89 32-01-89' ('<age> <gender> <weight> <unspecified>')
        # For some records, age/weight is given as 'x'.
        age, _, weight, _ = record.comments[0].split()
        subject_data = SubjectData(
            gender=Gender.MALE,  # all slpdb subjects were male
            age=None if age == "x" else int(age),
            weight=None if weight == "x" else int(weight),
        )

        yield SleepRecord(
            sleep_stages=sleep_stages,
            sleep_stage_duration=30,
            id=record_id,
            recording_start_time=start_time,
            heartbeat_times=heartbeat_times,
            subject_data=subject_data,
        )


def read_shhs(
    records_pattern: str = "*",
    heartbeats_source: str = "annotation",
    offline: bool = False,
    keep_edfs: bool = False,
    data_dir: str | Path | None = None,
) -> Iterator[SleepRecord]:
    """
    Lazily read records from [SHHS](https://sleepdata.org/datasets/shhs).

    Each SHHS record consists of an `.edf` file containing raw polysomnography data and an
    `.xml` file containing annotated events. Since the entire SHHS dataset requires about
    356 GB of disk space, `.edf` files can be deleted after heartbeat times have been
    extracted. Heartbeat times are cached in an `.npy` file in
    `<data_dir>/shhs/preprocessed/heartbeats`.

    Parameters
    ----------
    records_pattern : str, optional
         Glob-like pattern to select record IDs, by default `'*'`.
    heartbeats_source : {'annotation', 'cached', 'ecg'}, optional
        If `'annotation'` (default), get heartbeat times from
        `polysomnography/annotations-rpoints/shhsX/<record_id>-rpoints.csv`
        (not available for all records). If `'ecg'`, use `sleepecg.detect_heartbeats()` on
        the ECG contained in `polysomnography/edfs/shhsX/<record_id>.edf` and cache the
        result in `preprocessed/heartbeats/shhsX/<record_id>.npy`. If `'cached'`, get the
        cached heartbeats.
    offline : bool, optional
        If `True`, search for local files only instead of using the NSRR API, by default
        `False`.
    keep_edfs : bool, optional
        If `False`, remove `.edf` after heartbeat detection, by default `False`.
    data_dir : str | pathlib.Path, optional
        Directory where all datasets are stored. If `None` (default), the value will be
        taken from the configuration.

    Yields
    ------
    SleepRecord
        Each element in the generator is of type `SleepRecord`.
    """
    from edfio import read_edf

    DB_SLUG = "shhs"
    ANNOTATION_DIRNAME = "polysomnography/annotations-events-nsrr"
    EDF_DIRNAME = "polysomnography/edfs"
    HEARTBEATS_DIRNAME = "preprocessed/heartbeats"
    RPOINTS_DIRNAME = "polysomnography/annotations-rpoints"

    # see shhs/datasets/shhs-data-dictionary-0.16.0-domains.csv lines 91+92
    GENDER_MAPPING = {"2": Gender.FEMALE, "1": Gender.MALE}

    heartbeats_source_options = {"annotation", "cached", "ecg"}
    if heartbeats_source not in heartbeats_source_options:
        raise ValueError(
            f"Invalid value for parameter `heartbeats_source`: {heartbeats_source}, "
            f"possible options: {heartbeats_source_options}"
        )

    if data_dir is None:
        data_dir = get_config_value("data_dir")

    data_dir = Path(data_dir).expanduser()
    db_dir = data_dir / DB_SLUG
    annotations_dir = db_dir / ANNOTATION_DIRNAME
    edf_dir = db_dir / EDF_DIRNAME
    heartbeats_dir = db_dir / HEARTBEATS_DIRNAME

    for directory in (annotations_dir, edf_dir, heartbeats_dir):
        directory.mkdir(parents=True, exist_ok=True)

    if not offline:
        download_url = _get_nsrr_url(DB_SLUG)

        download_nsrr(
            DB_SLUG,
            "datasets",
            "shhs?-dataset-*.csv",
            shallow=True,
            data_dir=data_dir,
        )

        xml_files = _list_nsrr(
            DB_SLUG,
            ANNOTATION_DIRNAME,
            f"{records_pattern}-nsrr.xml",
            shallow=False,
        )
        checksums = dict(xml_files)
        requested_records = [file[-27:-9] for file, _ in xml_files]

        edf_files = _list_nsrr(
            DB_SLUG,
            EDF_DIRNAME,
            f"{records_pattern}.edf",
            shallow=False,
        )
        checksums.update(edf_files)

        rpoints_files = _list_nsrr(
            DB_SLUG,
            RPOINTS_DIRNAME,
            f"{records_pattern}-rpoint.csv",
            shallow=False,
        )
        checksums.update(rpoints_files)
    else:
        xml_paths = sorted(annotations_dir.rglob(f"{records_pattern}-nsrr.xml"))
        requested_records = [str(file)[-27:-9] for file in xml_paths]

    subject_data = {}

    if any(r.startswith("shhs1") for r in requested_records):
        subject_data_file_shhs1 = next((db_dir / "datasets").glob("shhs1-dataset-*.csv"))
        with open(subject_data_file_shhs1, newline="", encoding="windows-1252") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                record_id = f"shhs1-{row['nsrrid']}"
                subject_data[record_id] = SubjectData(
                    gender=GENDER_MAPPING.get(row["gender"]),
                    age=int(row["age_s1"]) if row["age_s1"] != "" else None,
                    weight=float(row["weight"]) if row["weight"] else None,
                )
    if any(r.startswith("shhs2") for r in requested_records):
        subject_data_file_shhs2 = next((db_dir / "datasets").glob("shhs2-dataset-*.csv"))
        with open(subject_data_file_shhs2, newline="", encoding="windows-1252") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                record_id = f"shhs2-{row['nsrrid']}"
                subject_data[record_id] = SubjectData(
                    gender=GENDER_MAPPING.get(row["gender"]),
                    age=int(row["age_s2"]) if row["age_s2"] != "" else None,
                    weight=None,  # subject weight was not recorded in shhs2
                )

    for record_id in requested_records:
        heartbeats_file = heartbeats_dir / f"{record_id}.npy"
        if heartbeats_source == "annotation":
            rpoints_filename = f"{RPOINTS_DIRNAME}/{record_id}-rpoint.csv"
            rpoints_filepath = db_dir / rpoints_filename
            if not rpoints_filepath.is_file():
                if not offline and rpoints_filename in checksums:
                    _download_nsrr_file(
                        download_url + rpoints_filename,
                        rpoints_filepath,
                        checksums[rpoints_filename],
                    )
                else:
                    print(f"Skipping {record_id} due to missing heartbeat annotations.")
                    continue
            heartbeat_times = np.loadtxt(
                rpoints_filepath,
                delimiter=",",
                skiprows=1,
                usecols=19,  # column 19 ('seconds') contains the annotated heartbeat times
            )
        elif heartbeats_source == "cached":
            if not heartbeats_file.is_file():
                print(f"Skipping {record_id} due to missing cached heartbeats.")
                continue
        elif heartbeats_source == "ecg":
            edf_filename = EDF_DIRNAME + f"/{record_id}.edf"
            edf_filepath = db_dir / edf_filename
            edf_was_available = edf_filepath.is_file()
            if not offline:
                _download_nsrr_file(
                    download_url + edf_filename,
                    edf_filepath,
                    checksums[edf_filename],
                )

            ecg = read_edf(edf_filepath).get_signal("ECG")
            heartbeat_indices = detect_heartbeats(ecg.data, ecg.sampling_frequency)
            heartbeat_times = heartbeat_indices / ecg.sampling_frequency

            heartbeats_file.parent.mkdir(parents=True, exist_ok=True)
            np.save(heartbeats_file, heartbeat_times)

            if not edf_was_available and not keep_edfs:
                edf_filepath.unlink()

        xml_filename = ANNOTATION_DIRNAME + f"/{record_id}-nsrr.xml"
        xml_filepath = db_dir / xml_filename
        if not offline:
            _download_nsrr_file(
                download_url + xml_filename,
                xml_filepath,
                checksums[xml_filename],
            )

        parsed_xml = _parse_nsrr_xml(xml_filepath)

        yield SleepRecord(
            sleep_stages=parsed_xml.sleep_stages,
            sleep_stage_duration=parsed_xml.sleep_stage_duration,
            id=record_id[6:],  # remove subdirectory
            recording_start_time=parsed_xml.recording_start_time,
            heartbeat_times=heartbeat_times,
            subject_data=subject_data[record_id[6:]],  # remove subdirectory]
        )
