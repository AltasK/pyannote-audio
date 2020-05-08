#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2020 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr


from typing import Text, Union
from pathlib import Path

import numpy as np
from pyannote.pipeline import Pipeline
from pyannote.pipeline.parameter import Uniform
from pyannote.pipeline.parameter import LogUniform

from pyannote.audio.features.wrapper import Wrapper
from pyannote.database.protocol.protocol import ProtocolFile
from pyannote.database import get_annotated
from pyannote.metrics.diarization import DiarizationErrorRate
from pyannote.core import Annotation
from pyannote.core import Timeline
from pyannote.core import Segment
from pyannote.core import SlidingWindowFeature
from pyannote.core.utils.numpy import one_hot_encoding
from pyannote.core.utils.numpy import one_hot_decoding
from pyannote.audio.utils.signal import Binarize
from pyannote.audio.utils.signal import Peak
from pyannote.core.utils.hierarchy import pool
from scipy.cluster.hierarchy import fcluster
from scipy.spatial.distance import cdist


class SimpleDiarization(Pipeline):
    """Simple diarization pipeline

    Parameters
    ----------
    sad : str or Path, optional
        Pretrained speech activity detection model. Defaults to "sad".
    scd : str or Path, optional
        Pretrained speaker change detection model. Defaults to "scd".
    emb : str or Path, optional
        Pretrained speaker embedding model. Defaults to "emb".

    Hyper-parameters
    ----------------
    sad_threshold_on, sad_threshold_off : float
        Thresholds applied on speech activity detection scores.
    scd_threshold : float
        Threshold applied on speaker change detection scores local maxima.
    seg_min_duration : float
        Minimum duration of speech turns.
    gap_min_duration : float
        Minimum duration of gaps between speech turns.
    emb_duration : float
        Do not cluster segments shorter than `emb_duration` duration. Short
        segments will eventually be assigned to the most similar cluster.
    emb_threshold : float
        Distance threshold used as stopping criterion for hierarchical
        agglomeratice clustering.
    """

    def __init__(
        self,
        sad: Union[Text, Path] = "sad",
        scd: Union[Text, Path] = "scd",
        emb: Union[Text, Path] = "emb",
    ):

        super().__init__()

        self.sad = Wrapper(sad)
        self.sad_speech_index_ = self.sad.classes.index("speech")

        self.scd = Wrapper(scd)
        self.scd_change_index_ = self.scd.classes.index("change")
        self.emb = Wrapper(emb)

        self.sad_threshold_on = Uniform(0.0, 1.0)
        self.sad_threshold_off = Uniform(0.0, 1.0)
        self.scd_threshold = LogUniform(1e-8, 1.0)
        self.seg_min_duration = Uniform(0.0, 0.5)
        self.gap_min_duration = Uniform(0.0, 0.5)
        self.emb_duration = Uniform(0.5, 4.0)
        self.emb_threshold = Uniform(0.0, 2.0)

    def initialize(self):

        self.sad_binarize_ = Binarize(
            onset=self.sad_threshold_on,
            offset=self.sad_threshold_off,
            min_duration_on=self.seg_min_duration,
            min_duration_off=self.gap_min_duration,
        )

        self.scd_peak_ = Peak(
            alpha=self.scd_threshold, min_duration=self.seg_min_duration
        )

    def get_embedding(self, current_file, segment):

        try:
            embeddings = self.emb.crop(current_file, segment)

        except RuntimeError as e:

            # A RuntimeError exception is raised by the pretrained "emb" model
            # when the input waveform is too short (i.e. < 157ms).

            # We catch it and extend the segment on both sides until it reaches
            # this target duration.

            # This ugly hack will probably bite us later...

            MIN_DURATION = 0.157
            if segment.middle - 0.5 * MIN_DURATION < 0.0:
                extended_segment = Segment(0.0, MIN_DURATION)
            elif segment.middle + 0.5 * MIN_DURATION > current_file["duration"]:
                extended_segment = Segment(
                    current_file["duration"] - MIN_DURATION, current_file["duration"]
                )
            else:
                extended_segment = Segment(
                    segment.middle - 0.5 * MIN_DURATION,
                    segment.middle + 0.5 * MIN_DURATION,
                )
            embeddings = self.emb.crop(current_file, extended_segment)

        return np.mean(embeddings, axis=0)

    def __call__(self, current_file: ProtocolFile) -> Annotation:

        uri = current_file.get("uri", "pyannote")

        # apply pretrained SAD model and turn log-probabilities into probabilities
        if "sad_scores" in current_file:
            sad_scores = current_file["sad_scores"]
        else:
            sad_scores = self.sad(current_file)
            if np.nanmean(sad_scores) < 0:
                sad_scores = np.exp(sad_scores)
            current_file["sad_scores"] = sad_scores

        # apply SAD binarization
        sad = self.sad_binarize_.apply(sad_scores, dimension=self.sad_speech_index_)

        # apply pretrained SCD model and turn log-probabilites into probabilites
        if "scd_scores" in current_file:
            scd_scores = current_file["scd_scores"]
        else:
            scd_scores = self.scd(current_file)
            if np.nanmean(scd_scores) < 0:
                scd_scores = np.exp(scd_scores)
            current_file["scd_scores"] = scd_scores

        # apply SCD peak detection
        scd = self.scd_peak_.apply(scd_scores, dimension=self.scd_change_index_)

        # split (potentially multi-speaker) speech regions at change points
        seg = scd.crop(sad, mode="intersection")

        # remove resulting tiny segments, inconsistent with SCD duration constraint
        seg = [s for s in seg if s.duration > min(0.1, self.seg_min_duration)]

        # separate long segments from short ones
        seg_long = [s for s in seg if s.duration >= self.emb_duration]

        if len(seg_long) == 0:
            # there are only short segments. put each of them in its own cluster
            return Timeline(segments=seg, uri=uri).to_annotation(generator="string")

        elif len(seg_long) == 1:
            # there is exactly one long segment. put everything in one cluster
            return Timeline(segments=seg, uri=uri).to_annotation(
                generator=iter(lambda: "A", None)
            )

        else:

            # extract embeddings of long segments
            emb_long = np.vstack(
                [self.get_embedding(current_file, s) for s in seg_long]
            )

            # apply clustering
            Z = pool(
                emb_long,
                metric="cosine",
                # pooling_func="average",
                # cannot_link=None,
                # must_link=None,
            )
            cluster_long = fcluster(Z, self.emb_threshold, criterion="distance")

            seg_shrt = [s for s in seg if s.duration < self.emb_duration]

            if len(seg_shrt) == 0:
                # there are only long segments.
                return Timeline(segments=seg, uri=uri).to_annotation(
                    generator=iter(cluster_long)
                )

            # extract embeddings of short segments
            emb_shrt = np.vstack(
                [self.get_embedding(current_file, s) for s in seg_shrt]
            )

            # assign each short segment to the cluster containing the closest long segment
            cluster_shrt = cluster_long[
                np.argmin(cdist(emb_long, emb_shrt, metric="cosine"), axis=0)
            ]

            seg_shrt = Timeline(segments=seg_shrt, uri=uri)
            seg_long = Timeline(segments=seg_long, uri=uri)

            return seg_long.to_annotation(generator=iter(cluster_long)).update(
                seg_shrt.to_annotation(generator=iter(cluster_shrt))
            )

    def loss(self, current_file: ProtocolFile, hypothesis: Annotation) -> float:
        """Compute diarization error rate

        Parameters
        ----------
        current_file : ProtocolFile
            Protocol file
        hypothesis : Annotation
            Hypothesized diarization output

        Returns
        -------
        der : float
            Diarization error rate.
        """

        return DiarizationErrorRate()(
            current_file["annotation"], hypothesis, uem=get_annotated(current_file)
        )

    def get_metric(self) -> DiarizationErrorRate:
        return DiarizationErrorRate(collar=0.0, skip_overlap=False)


class DiscreteDiarization(Pipeline):
    """Very simple diarization pipeline

    Parameters
    ----------
    sad : str or Path, optional
        Pretrained speech activity detection model. Defaults to "sad".
    emb : str or Path, optional
        Pretrained speaker embedding model. Defaults to "emb".
    ovl : str or Path, optional
        Pretrained overlapped speech detection model.
        Default behavior is to not use overlapped speech detection.
    batch_size : int, optional
        Batch size.

    Hyper-parameters
    ----------------
    sad_threshold_on, sad_threshold_off : float
        Onset/offset speech activity detection thresholds.
    sad_min_duration_on, sad_min_duration_off : float
        Minimum duration of speech/non-speech regions.
    emb_duration, emb_step_ratio : float
        Sliding window used for embedding extraction.
    emb_threshold : float
        Distance threshold used as stopping criterion for hierarchical
        agglomeratice clustering.
    """

    def __init__(
        self,
        sad: Union[Text, Path] = "sad",
        emb: Union[Text, Path] = "emb",
        ovl: Union[Text, Path] = None,
        batch_size: int = None,
    ):

        super().__init__()

        self.sad = Wrapper(sad)
        if batch_size is not None:
            self.sad.batch_size = batch_size
        self.sad_speech_index_ = self.sad.classes.index("speech")

        self.sad_threshold_on = Uniform(0.0, 1.0)
        self.sad_threshold_off = Uniform(0.0, 1.0)
        self.sad_min_duration_on = Uniform(0.0, 0.5)
        self.sad_min_duration_off = Uniform(0.0, 0.5)

        self.emb = Wrapper(emb)
        if batch_size is not None:
            self.emb.batch_size = batch_size

        max_duration = self.emb.duration
        min_duration = getattr(self.emb, "min_duration", 0.5 * max_duration)
        self.emb_duration = Uniform(min_duration, max_duration)
        self.emb_step_ratio = Uniform(0.1, 1.0)
        self.emb_threshold = Uniform(0.0, 2.0)

        if ovl is None:
            self.ovl = None
        else:
            self.ovl = Wrapper(ovl)
            if batch_size is not None:
                self.ovl.batch_size = batch_size
            self.ovl_overlap_index_ = self.ovl.classes.index("overlap")

            self.ovl_threshold_on = Uniform(0.0, 1.0)
            self.ovl_threshold_off = Uniform(0.0, 1.0)
            self.ovl_min_duration_on = Uniform(0.0, 0.5)
            self.ovl_min_duration_off = Uniform(0.0, 0.5)

    def initialize(self):

        self.sad_binarize_ = Binarize(
            onset=self.sad_threshold_on,
            offset=self.sad_threshold_off,
            min_duration_on=self.sad_min_duration_on,
            min_duration_off=self.sad_min_duration_off,
        )

        # embeddings will be extracted with a sliding window
        # of "emb_duration" duration and "emb_step_ratio x emb_duration" step.
        self.emb.duration = self.emb_duration
        self.emb.step = self.emb_step_ratio

        if self.ovl is not None:
            self.ovl_binarize_ = Binarize(
                onset=self.ovl_threshold_on,
                offset=self.ovl_threshold_off,
                min_duration_on=self.ovl_min_duration_on,
                min_duration_off=self.ovl_min_duration_off,
            )

    def __call__(self, current_file: ProtocolFile) -> Annotation:

        uri = current_file.get("uri", "pyannote")
        extent = Segment(0, current_file["duration"])

        # speaker embedding
        emb: SlidingWindowFeature = self.emb(current_file)

        # speech activity detection
        if "sad_scores" in current_file:
            sad_scores: SlidingWindowFeature = current_file["sad_scores"]
        else:
            sad_scores: SlidingWindowFeature = self.sad(current_file)
            if np.nanmean(sad_scores) < 0:
                sad_scores = np.exp(sad_scores)
            current_file["sad_scores"] = sad_scores

        speech: Timeline = self.sad_binarize_.apply(
            sad_scores, dimension=self.sad_speech_index_
        )

        speech_discrete: np.ndarray = one_hot_encoding(
            speech.to_annotation(generator=iter(lambda: "speech", None)),
            Timeline(segments=[extent]),
            emb.sliding_window,
            labels=["speech",],
            mode="center",
        )[: len(emb)]

        speech_indices: np.ndarray = np.where(speech_discrete)[0]

        if len(speech_indices) == 0:
            return Annotation(uri=uri)

        if self.ovl is not None:

            # overlapped speech detection
            if "ovl_scores" in current_file:
                ovl_scores: SlidingWindowFeature = current_file["ovl_scores"]
            else:
                ovl_scores: SlidingWindowFeature = self.ovl(current_file)
                if np.nanmean(ovl_scores) < 0:
                    ovl_scores = np.exp(ovl_scores)
                current_file["ovl_scores"] = ovl_scores

            overlap: Timeline = self.ovl_binarize_.apply(
                ovl_scores, dimension=self.ovl_overlap_index_
            )

            clean_speech: Timeline = speech.crop(
                overlap.gaps(support=extent), mode="intersection"
            )
            noisy_speech: Timeline = speech.crop(overlap, mode="intersection")

            overlap_discrete = one_hot_encoding(
                overlap.to_annotation(generator=iter(lambda: "overlap", None)),
                Timeline(segments=[extent]),
                emb.sliding_window,
                labels=["overlap",],
                mode="center",
            )[: len(emb)]

            overlap_indices: np.ndarray = np.where(overlap_discrete)[0]

            clean_speech_indices = np.where(speech_discrete * (1 - overlap_discrete))[0]
            noisy_speech_indices = np.where(speech_discrete * overlap_discrete)[0]

        # TODO. use overlap speech detection to not use overlap regions for initial clustering

        # hierarchical agglomerative clustering
        indices = speech_indices if self.ovl is None else clean_speech_indices
        dendrogram = pool(emb[indices], metric="cosine")
        cluster = fcluster(dendrogram, self.emb_threshold, criterion="distance") - 1
        num_clusters = np.max(cluster) + 1
        y = np.zeros((len(emb), num_clusters), dtype=np.int8)
        for i, k in zip(indices, cluster):
            y[i, k] = 1

        hypothesis: Annotation = one_hot_decoding(
            y, emb.sliding_window, labels=list(range(num_clusters))
        )
        hypothesis.uri = uri
        if self.ovl is None:
            hypothesis = hypothesis.crop(speech, mode="intersection")

        else:
            hypothesis = hypothesis.crop(clean_speech, mode="intersection")

        if self.ovl is not None:
            # assign each overlap embedding to 2 most similar clusters (where
            # each cluster is represented by its average embedding)
            cluster_emb = np.vstack(
                [
                    np.mean(emb[indices][cluster == k], axis=0)
                    for k in range(num_clusters)
                ]
            )
            distance = cdist(cluster_emb, emb[noisy_speech_indices], metric="cosine")
            most_similar_cluster_indices = np.argpartition(
                distance, min(2, num_clusters), axis=0,
            )[: min(2, num_clusters)]

            y = np.zeros((len(emb), num_clusters), dtype=np.int8)
            for i, ks in zip(noisy_speech_indices, most_similar_cluster_indices.T):
                for k in ks:
                    y[i, k] = 1

            noisy_hypothesis: Annotation = one_hot_decoding(
                y, emb.sliding_window, labels=list(range(num_clusters))
            )
            noisy_hypothesis = noisy_hypothesis.crop(noisy_speech, mode="intersection")
            hypothesis = hypothesis.update(noisy_hypothesis)

        return hypothesis

    def loss(self, current_file: ProtocolFile, hypothesis: Annotation) -> float:
        """Compute diarization error rate

        Parameters
        ----------
        current_file : ProtocolFile
            Protocol file
        hypothesis : Annotation
            Hypothesized diarization output

        Returns
        -------
        der : float
            Diarization error rate.
        """

        return DiarizationErrorRate()(
            current_file["annotation"], hypothesis, uem=get_annotated(current_file)
        )

    def get_metric(self) -> DiarizationErrorRate:
        return DiarizationErrorRate(collar=0.0, skip_overlap=False)