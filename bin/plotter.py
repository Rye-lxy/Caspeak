#! /usr/bin/env python
# Author: Martin C. Frith 2008
# SPDX-License-Identifier: GPL-3.0-or-later

# Read pair-wise alignments: write an "Oxford grid", a.k.a. dotplot.

# TODO: Currently, pixels with zero aligned nt-pairs are white, and
# pixels with one or more aligned nt-pairs are black.  This can look
# too crowded for large genome alignments.  I tried shading each pixel
# according to the number of aligned nt-pairs within it, but the
# result is too faint.  How can this be done better?

import collections
import functools
import gzip
from fnmatch import fnmatchcase
import logging
from operator import itemgetter
import subprocess
import itertools, optparse, os, re, sys

from fileReader import openFile

# Try to make PIL/PILLOW work:
try:
    from PIL import Image, ImageDraw, ImageFont, ImageColor
except ImportError:
    import Image, ImageDraw, ImageFont, ImageColor

try:
    from future_builtins import zip
except ImportError:
    pass

def groupByFirstItem(things):
    for k, v in itertools.groupby(things, itemgetter(0)):
        yield k, [i[1:] for i in v]

def commaSeparatedInts(text):
    return map(int, text.rstrip(",").split(","))

def croppedBlocks(blocks, ranges1, ranges2):
    headBeg1, headBeg2, headSize = blocks[0]
    for r1 in ranges1:
        for r2 in ranges2:
            cropBeg1, cropEnd1 = r1
            if headBeg1 < 0:
                cropBeg1, cropEnd1 = -cropEnd1, -cropBeg1
            cropBeg2, cropEnd2 = r2
            if headBeg2 < 0:
                cropBeg2, cropEnd2 = -cropEnd2, -cropBeg2
            for beg1, beg2, size in blocks:
                b1 = max(cropBeg1, beg1)
                e1 = min(cropEnd1, beg1 + size)
                if b1 >= e1: continue
                offset = beg2 - beg1
                b2 = max(cropBeg2, b1 + offset)
                e2 = min(cropEnd2, e1 + offset)
                if b2 >= e2: continue
                yield b2 - offset, b2, e2 - b2

def tabBlocks(blocks, beg1, beg2, sizeMul, seq1mul, seq2mul):
    '''Get the gapless blocks of an alignment, from LAST tabular format.'''
    for i in blocks:
        if len(i) > 1:
            beg1 += i[0]
            beg2 += i[1]
        else:
            size = i[0]
            yield beg1 * seq1mul, beg2 * seq2mul, size * sizeMul
            beg1 += size * seq2mul
            beg2 += size * seq1mul

def mafBlocks(beg1, beg2, seq1, seq2):
    '''Get the gapless blocks of an alignment, from MAF format.'''
    size = 0
    for x, y in zip(seq1, seq2):
        if x == "-":
            if size:
                yield beg1, beg2, size
                beg1 += size
                beg2 += size
                size = 0
            beg2 += 1
        elif y == "-":
            if size:
                yield beg1, beg2, size
                beg1 += size
                beg2 += size
                size = 0
            beg1 += 1
        else:
            size += 1
    if size: yield beg1, beg2, size

def alignmentFromSegment(qrySeqName, qrySeqLen, segment):
    refSeqLen = sys.maxsize  # XXX
    refSeqName, refSeqBeg, qrySeqBeg, size = segment
    block = refSeqBeg, qrySeqBeg, size
    return refSeqName, refSeqLen, qrySeqName, qrySeqLen, [block]

def dataFromPsl(strand, seqName, seqLen, alnBeg, alnEnd, blockBegs, blockLens):
    seqLen = int(seqLen)
    blockBegs = list(commaSeparatedInts(blockBegs))
    if strand == "+":
        end = int(alnEnd)
    else:
        end = -int(alnBeg)
        blockBegs = [i - seqLen for i in blockBegs]
    isTranslatedDna = blockBegs[-1] + blockLens[-1] < end
    return seqName, seqLen, blockBegs, isTranslatedDna

def dataFromTab(blockSum, seqName, alnBeg, span, strand, seqLen):
    alnBeg = int(alnBeg)
    seqLen = int(seqLen)
    if strand == "-": alnBeg -= seqLen
    isTranslatedDna = blockSum < int(span)
    return seqName, seqLen, alnBeg, isTranslatedDna

def dataFromMaf(junk, seqName, alnBeg, span, strand, seqLen, alnSeq):
    alnBeg = int(alnBeg)
    seqLen = int(seqLen)
    if strand == "-": alnBeg -= seqLen
    if int(span) < len(alnSeq) - alnSeq.count("-"):
        alnBeg *= 3  # protein coordinate -> DNA coordinate
        seqLen *= 3  # protein length -> DNA length
    return seqName, seqLen, alnBeg, alnSeq

def aaToNtFactors(isTranslatedSeq1, isTranslatedSeq2):
    if isTranslatedSeq1 and isTranslatedSeq2: return 3, 1, 1
    if isTranslatedSeq1: return 3, 1, 3  # seq1 is DNA, seq2 is protein
    if isTranslatedSeq2: return 3, 3, 1  # seq2 is DNA, seq1 is protein
    return 1, 1, 1

def alignmentInput(lines):
    '''Read alignments and sequence lengths.'''
    mafCount = 0
    qrySeqName = ""
    segments = []
    for line in lines:
        w = line.split()
        if line[0] == "#":
            pass
        elif len(w) == 1:
            for i in segments:
                yield alignmentFromSegment(qrySeqName, qrySeqLen, i)
            qrySeqName = w[0]
            qrySeqLen = 0
            segments = []
        elif len(w) == 2 and qrySeqName and w[1].isdigit():
            qrySeqLen += int(w[1])
        elif len(w) == 4 and qrySeqName and w[1].isdigit() and w[3].isdigit():
            refSeqName, refSeqBeg, refSeqEnd = w[0], int(w[1]), int(w[3])
            size = abs(refSeqEnd - refSeqBeg)
            if refSeqBeg > refSeqEnd:
                refSeqBeg = -refSeqBeg
            segments.append((refSeqName, refSeqBeg, qrySeqLen, size))
            qrySeqLen += size
        elif len(w) > 20:  # PSL format
            strand = w[8]
            strand2 = strand[0]
            strand1 = strand[1] if len(strand) > 1 else "+"
            sizes = list(commaSeparatedInts(w[18]))
            d1 = dataFromPsl(strand1, w[13], w[14], w[15], w[16], w[20], sizes)
            d2 = dataFromPsl(strand2, w[ 9], w[10], w[11], w[12], w[19], sizes)
            chr1, seqlen1, beg1s, isTransDna1 = d1
            chr2, seqlen2, beg2s, isTransDna2 = d2
            sizeMul, seq1mul, seq2mul = aaToNtFactors(isTransDna1, isTransDna2)
            sizes = [i * sizeMul for i in sizes]
            beg1s = [i * seq1mul for i in beg1s]
            beg2s = [i * seq2mul for i in beg2s]
            blocks = zip(beg1s, beg2s, sizes)
            yield chr1, seqlen1 * seq1mul, chr2, seqlen2 * seq2mul, blocks
        elif line[0].isdigit():  # tabular format
            blocks = w[11].split(",")
            blocks = [[int(j) for j in i.split(":")] for i in blocks]
            blockSum1 = sum(i[0] for i in blocks)
            blockSum2 = sum(i[-1] for i in blocks)
            chr1, seqlen1, beg1, isTransDna1 = dataFromTab(blockSum1, *w[1:6])
            chr2, seqlen2, beg2, isTransDna2 = dataFromTab(blockSum2, *w[6:11])
            sizeMul, seq1mul, seq2mul = aaToNtFactors(isTransDna1, isTransDna2)
            blocks = tabBlocks(blocks, beg1, beg2, sizeMul, seq1mul, seq2mul)
            yield chr1, seqlen1 * seq1mul, chr2, seqlen2 * seq2mul, blocks
        elif line[0] == "s":  # MAF format
            if mafCount == 0:
                chr1, seqlen1, beg1, seq1 = dataFromMaf(*w)
                mafCount = 1
            else:
                chr2, seqlen2, beg2, seq2 = dataFromMaf(*w)
                blocks = mafBlocks(beg1, beg2, seq1, seq2)
                yield chr1, seqlen1, chr2, seqlen2, blocks
                mafCount = 0
    for i in segments:
        yield alignmentFromSegment(qrySeqName, qrySeqLen, i)

def seqRequestFromText(text):
    s = text.split()
    if len(s) == 3:
        return s[0], int(s[1]), int(s[2])
    if ":" in text:
        pattern, interval = text.rsplit(":", 1)
        if "-" in interval:
            beg, end = interval.rsplit("-", 1)
            return pattern, int(beg), int(end)  # beg may be negative
    return text, 0, sys.maxsize

def rangesFromSeqName(seqRequests, name, seqLen):
    if seqRequests:
        base = name.split(".", 1)[-1]  # allow for names like hg19.chr7
        for pat, beg, end in seqRequests:
            if fnmatchcase(name, pat) or fnmatchcase(base, pat):
                yield max(beg, 0), min(end, seqLen)
    else:
        yield 0, seqLen

def updateSeqs(coverDict, seqRanges, seqName, ranges, coveredRange):
    beg, end = coveredRange
    if beg < 0:
        coveredRange = -end, -beg
    if seqName in coverDict:
        coverDict[seqName].append(coveredRange)
    else:
        coverDict[seqName] = [coveredRange]
        for beg, end in ranges:
            r = seqName, beg, end
            seqRanges.append(r)

def readAlignments(fileName, opts, split=False):
    '''Read alignments and sequence limits.'''
    seqRequests1 = [seqRequestFromText(i) for i in opts.seq1]
    seqRequests2 = [seqRequestFromText(i) for i in opts.seq2]

    alignments = []
    seqRanges1 = []
    seqRanges2 = []
    coverDict1 = {}
    coverDict2 = {}
    splitBy = ""
    lines = openFile(fileName)
    for seqName1, seqLen1, seqName2, seqLen2, blocks in alignmentInput(lines):
        if splitBy == "":
            splitBy = seqName2
        elif splitBy != seqName2 and split:
            yield alignments, seqRanges1, coverDict1, seqRanges2, coverDict2
            alignments = []
            seqRanges1 = []
            seqRanges2 = []
            coverDict1 = {}
            coverDict2 = {}
            splitBy = seqName2
        ranges1 = sorted(rangesFromSeqName(seqRequests1, seqName1, seqLen1))
        if not ranges1: continue
        ranges2 = sorted(rangesFromSeqName(seqRequests2, seqName2, seqLen2))
        if not ranges2: continue
        b = list(croppedBlocks(list(blocks), ranges1, ranges2))
        if not b: continue
        aln = seqName1, seqName2, b
        alignments.append(aln)
        coveredRange1 = b[0][0], b[-1][0] + b[-1][2]
        updateSeqs(coverDict1, seqRanges1, seqName1, ranges1, coveredRange1)
        coveredRange2 = b[0][1], b[-1][1] + b[-1][2]
        updateSeqs(coverDict2, seqRanges2, seqName2, ranges2, coveredRange2)
    
    if split:
        yield alignments, seqRanges1, coverDict1, seqRanges2, coverDict2
    else:
        return alignments, seqRanges1, coverDict1, seqRanges2, coverDict2

def nameAndRangesFromDict(cropDict, seqName):
    if seqName in cropDict:
        return seqName, cropDict[seqName]
    n = seqName.split(".", 1)[-1]
    if n in cropDict:
        return n, cropDict[n]
    return seqName, []

def rangesForSecondaryAlignments(primaryRanges, seqLen):
    if primaryRanges:
        return primaryRanges
    return [(0, seqLen)]

def readSecondaryAlignments(opts, cropRanges1, cropRanges2):
    cropDict1 = dict(groupByFirstItem(cropRanges1))
    cropDict2 = dict(groupByFirstItem(cropRanges2))

    alignments = []
    seqRanges1 = []
    seqRanges2 = []
    coverDict1 = {}
    coverDict2 = {}
    lines = openFile(opts.alignments)
    for seqName1, seqLen1, seqName2, seqLen2, blocks in alignmentInput(lines):
        seqName1, ranges1 = nameAndRangesFromDict(cropDict1, seqName1)
        seqName2, ranges2 = nameAndRangesFromDict(cropDict2, seqName2)
        if not ranges1 and not ranges2:
            continue
        r1 = rangesForSecondaryAlignments(ranges1, seqLen1)
        r2 = rangesForSecondaryAlignments(ranges2, seqLen2)
        b = list(croppedBlocks(list(blocks), r1, r2))
        if not b: continue
        aln = seqName1, seqName2, b
        alignments.append(aln)
        if not ranges1:
            coveredRange1 = b[0][0], b[-1][0] + b[-1][2]
            updateSeqs(coverDict1, seqRanges1, seqName1, r1, coveredRange1)
        if not ranges2:
            coveredRange2 = b[0][1], b[-1][1] + b[-1][2]
            updateSeqs(coverDict2, seqRanges2, seqName2, r2, coveredRange2)
    return alignments, seqRanges1, coverDict1, seqRanges2, coverDict2

def twoValuesFromOption(text, separator):
    if separator in text:
        return text.split(separator)
    return text, text

def mergedRanges(ranges):
    oldBeg, maxEnd = ranges[0]
    for beg, end in ranges:
        if beg > maxEnd:
            yield oldBeg, maxEnd
            oldBeg = beg
            maxEnd = end
        elif end > maxEnd:
            maxEnd = end
    yield oldBeg, maxEnd

def mergedRangesPerSeq(coverDict):
    for k, v in coverDict.items():
        v.sort()
        yield k, list(mergedRanges(v))

def coveredLength(mergedCoverDict):
    return sum(sum(e - b for b, e in v) for v in mergedCoverDict.values())

def trimmed(seqRanges, coverDict, minAlignedBases, maxGapFrac, endPad, midPad):
    maxEndGapFrac, maxMidGapFrac = twoValuesFromOption(maxGapFrac, ",")
    maxEndGap = max(float(maxEndGapFrac) * minAlignedBases, endPad * 1.0)
    maxMidGap = max(float(maxMidGapFrac) * minAlignedBases, midPad * 2.0)

    for seqName, rangeBeg, rangeEnd in seqRanges:
        seqBlocks = coverDict[seqName]
        blocks = [i for i in seqBlocks if i[0] < rangeEnd and i[1] > rangeBeg]
        if blocks[0][0] - rangeBeg > maxEndGap:
            rangeBeg = blocks[0][0] - endPad
        for j, y in enumerate(blocks):
            if j:
                x = blocks[j - 1]
                if y[0] - x[1] > maxMidGap:
                    yield seqName, rangeBeg, x[1] + midPad
                    rangeBeg = y[0] - midPad
        if rangeEnd - blocks[-1][1] > maxEndGap:
            rangeEnd = blocks[-1][1] + endPad
        yield seqName, rangeBeg, rangeEnd

def rangesWithStrandInfo(seqRanges, strandOpt, alignments, seqIndex):
    if strandOpt == "1":
        forwardMinusReverse = collections.defaultdict(int)
        for i in alignments:
            blocks = i[2]
            beg1, beg2, size = blocks[0]
            numOfAlignedLetterPairs = sum(i[2] for i in blocks)
            if (beg1 < 0) != (beg2 < 0):  # opposite-strand alignment
                numOfAlignedLetterPairs *= -1
            forwardMinusReverse[i[seqIndex]] += numOfAlignedLetterPairs
    strandNum = 0
    for seqName, beg, end in seqRanges:
        if strandOpt == "1":
            strandNum = 1 if forwardMinusReverse[seqName] >= 0 else 2
        yield seqName, beg, end, strandNum

def natural_sort_key(my_string):
    '''Return a sort key for "natural" ordering, e.g. chr9 < chr10.'''
    parts = re.split(r'(\d+)', my_string)
    parts[1::2] = map(int, parts[1::2])
    return parts

def nameKey(oneSeqRanges):
    return natural_sort_key(oneSeqRanges[0][0])

def sizeKey(oneSeqRanges):
    return sum(b - e for n, b, e, s in oneSeqRanges), nameKey(oneSeqRanges)

def alignmentKey(seqNamesToLists, oneSeqRanges):
    seqName = oneSeqRanges[0][0]
    alignmentsOfThisSequence = seqNamesToLists[seqName]
    numOfAlignedLetterPairs = sum(i[3] for i in alignmentsOfThisSequence)
    toMiddle = numOfAlignedLetterPairs // 2
    for i in alignmentsOfThisSequence:
        toMiddle -= i[3]
        if toMiddle < 0:
            return i[1:3]  # sequence-rank and "position" of this alignment

def rankAndFlipPerSeq(seqRanges):
    rangesGroupedBySeqName = itertools.groupby(seqRanges, itemgetter(0))
    for rank, group in enumerate(rangesGroupedBySeqName):
        seqName, ranges = group
        strandNum = next(ranges)[3]
        flip = 1 if strandNum < 2 else -1
        yield seqName, (rank, flip)

def alignmentSortData(alignments, seqIndex, otherNamesToRanksAndFlips):
    otherIndex = 1 - seqIndex
    for i in alignments:
        blocks = i[2]
        otherRank, otherFlip = otherNamesToRanksAndFlips[i[otherIndex]]
        otherPos = otherFlip * abs(blocks[0][otherIndex] +
                                   blocks[-1][otherIndex] + blocks[-1][2])
        numOfAlignedLetterPairs = sum(i[2] for i in blocks)
        yield i[seqIndex], otherRank, otherPos, numOfAlignedLetterPairs

def mySortedRanges(seqRanges, sortOpt, seqIndex, alignments, otherRanges):
    rangesGroupedBySeqName = itertools.groupby(seqRanges, itemgetter(0))
    g = [list(ranges) for seqName, ranges in rangesGroupedBySeqName]
    for i in g:
        if i[0][3] > 1:
            i.reverse()
    if sortOpt == "1":
        g.sort(key=nameKey)
    if sortOpt == "2":
        g.sort(key=sizeKey)
    if sortOpt == "3":
        otherNamesToRanksAndFlips = dict(rankAndFlipPerSeq(otherRanges))
        alns = sorted(alignmentSortData(alignments, seqIndex,
                                        otherNamesToRanksAndFlips))
        alnsGroupedBySeqName = itertools.groupby(alns, itemgetter(0))
        seqNamesToLists = dict((k, list(v)) for k, v in alnsGroupedBySeqName)
        g.sort(key=functools.partial(alignmentKey, seqNamesToLists))
    return [j for i in g for j in i]

def allSortedRanges(opts, alignments, alignmentsB,
                    seqRanges1, seqRangesB1, seqRanges2, seqRangesB2):
    o1, oB1 = twoValuesFromOption(opts.strands1, ":")
    o2, oB2 = twoValuesFromOption(opts.strands2, ":")
    if o1 == "1" and o2 == "1":
        raise RuntimeError("the strand options have circular dependency")
    seqRanges1 = list(rangesWithStrandInfo(seqRanges1, o1, alignments, 0))
    seqRanges2 = list(rangesWithStrandInfo(seqRanges2, o2, alignments, 1))
    seqRangesB1 = list(rangesWithStrandInfo(seqRangesB1, oB1, alignmentsB, 0))
    seqRangesB2 = list(rangesWithStrandInfo(seqRangesB2, oB2, alignmentsB, 1))

    o1, oB1 = twoValuesFromOption(opts.sort1, ":")
    o2, oB2 = twoValuesFromOption(opts.sort2, ":")
    if o1 == "3" and o2 == "3":
        raise RuntimeError("the sort options have circular dependency")
    if o1 != "3":
        s1 = mySortedRanges(seqRanges1, o1, None, None, None)
    if o2 != "3":
        s2 = mySortedRanges(seqRanges2, o2, None, None, None)
    if o1 == "3":
        s1 = mySortedRanges(seqRanges1, o1, 0, alignments, s2)
    if o2 == "3":
        s2 = mySortedRanges(seqRanges2, o2, 1, alignments, s1)
    t1 = mySortedRanges(seqRangesB1, oB1, 0, alignmentsB, s2)
    t2 = mySortedRanges(seqRangesB2, oB2, 1, alignmentsB, s1)
    return s1 + t1, s2 + t2

def myTextsize(textDraw, font, text):
    try:
        out = textDraw.textsize(text, font=font)
    except AttributeError:
        a, b, c, d = textDraw.textbbox((0, 0), text, font=font)
        out = c, d
    return out

def sizesPerText(texts, font, textDraw):
    sizes = 0, 0
    for t in texts:
        if textDraw is not None:
            sizes = myTextsize(textDraw, font, t)
        yield t, sizes

def prettyNum(n):
    t = str(n)
    groups = []
    while t:
        groups.append(t[-3:])
        t = t[:-3]
    return ",".join(reversed(groups))

def sizeText(size):
    suffixes = "bp", "kb", "Mb", "Gb"
    for i, x in enumerate(suffixes):
        j = 10 ** (i * 3)
        if size < j * 10:
            return "%.2g" % (1.0 * size / j) + x
        if size < j * 1000 or i == len(suffixes) - 1:
            return "%.0f" % (1.0 * size / j) + x

def labelText(seqRange, labelOpt):
    seqName, beg, end, strandNum = seqRange
    if labelOpt == 1:
        return seqName + ": " + sizeText(end - beg)
    if labelOpt == 2:
        return seqName + ":" + prettyNum(beg) + ": " + sizeText(end - beg)
    if labelOpt == 3:
        return seqName + ":" + prettyNum(beg) + "-" + prettyNum(end)
    return seqName

def rangeLabels(seqRanges, labelOpt, font, textDraw, textRot):
    x = y = 0
    for r in seqRanges:
        text = labelText(r, labelOpt)
        if textDraw is not None:
            x, y = myTextsize(textDraw, font, text)
            if textRot:
                x, y = y, x
        yield text, x, y, r[3]

def dataFromRanges(sortedRanges, font, textDraw, labelOpt, textRot):
    for seqName, rangeBeg, rangeEnd, strandNum in sortedRanges:
        out = [seqName, str(rangeBeg), str(rangeEnd)]
        if strandNum > 0:
            out.append(".+-"[strandNum])
        logging.info("\t".join(out))
    logging.info("")
    rangeSizes = [e - b for n, b, e, s in sortedRanges]
    labs = list(rangeLabels(sortedRanges, labelOpt, font, textDraw, textRot))
    margin = max(i[2] for i in labs)
    # xxx the margin may be too big, because some labels may get omitted
    return rangeSizes, labs, margin

def div_ceil(x, y):
    '''Return x / y rounded up.'''
    q, r = divmod(x, y)
    return q + (r != 0)

def get_bp_per_pix(rangeSizes, pixTweenRanges, maxPixels):
    '''Get the minimum bp-per-pixel that fits in the size limit.'''
    logging.info("choosing bp per pixel...")
    numOfRanges = len(rangeSizes)
    maxPixelsInRanges = maxPixels - pixTweenRanges * (numOfRanges - 1)
    if maxPixelsInRanges < numOfRanges:
        raise RuntimeError("can't fit the image: too many sequences?")
    negLimit = -maxPixelsInRanges
    negBpPerPix = sum(rangeSizes) // negLimit
    while True:
        if sum(i // negBpPerPix for i in rangeSizes) >= negLimit:
            return -negBpPerPix
        negBpPerPix -= 1

def getRangePixBegs(rangePixLens, pixTweenRanges, margin):
    '''Get the start pixel for each range.'''
    rangePixBegs = []
    pix_tot = margin - pixTweenRanges
    for i in rangePixLens:
        pix_tot += pixTweenRanges
        rangePixBegs.append(pix_tot)
        pix_tot += i
    return rangePixBegs

def pixelData(rangeSizes, bp_per_pix, pixTweenRanges, margin):
    '''Return pixel information about the ranges.'''
    rangePixLens = [div_ceil(i, bp_per_pix) for i in rangeSizes]
    rangePixBegs = getRangePixBegs(rangePixLens, pixTweenRanges, margin)
    tot_pix = rangePixBegs[-1] + rangePixLens[-1]
    return rangePixBegs, rangePixLens, tot_pix

def drawLineForward(hits, width, bp_per_pix, beg1, beg2, size):
    while True:
        q1, r1 = divmod(beg1, bp_per_pix)
        q2, r2 = divmod(beg2, bp_per_pix)
        hits[q2 * width + q1] |= 1
        next_pix = min(bp_per_pix - r1, bp_per_pix - r2)
        if next_pix >= size: break
        beg1 += next_pix
        beg2 += next_pix
        size -= next_pix

def drawLineReverse(hits, width, bp_per_pix, beg1, beg2, size):
    while True:
        q1, r1 = divmod(beg1, bp_per_pix)
        q2, r2 = divmod(beg2, bp_per_pix)
        hits[q2 * width + q1] |= 2
        next_pix = min(bp_per_pix - r1, r2 + 1)
        if next_pix >= size: break
        beg1 += next_pix
        beg2 -= next_pix
        size -= next_pix

def strandAndOrigin(ranges, beg, size):
    isReverseStrand = (beg < 0)
    if isReverseStrand:
        beg = -(beg + size)
    for rangeBeg, rangeEnd, isReverseRange, origin in ranges:
        if rangeEnd > beg:  # assumes the ranges are sorted
            return (isReverseStrand != isReverseRange), origin

def alignmentPixels(width, height, alignments, bp_per_pix,
                    rangeDict1, rangeDict2):
    hits = [0] * (width * height)  # the image data
    for seq1, seq2, blocks in alignments:
        beg1, beg2, size = blocks[0]
        isReverse1, ori1 = strandAndOrigin(rangeDict1[seq1], beg1, size)
        isReverse2, ori2 = strandAndOrigin(rangeDict2[seq2], beg2, size)
        for beg1, beg2, size in blocks:
            if isReverse1:
                beg1 = -(beg1 + size)
                beg2 = -(beg2 + size)
            if isReverse1 == isReverse2:
                drawLineForward(hits, width, bp_per_pix,
                                ori1 + beg1, ori2 + beg2, size)
            else:
                drawLineReverse(hits, width, bp_per_pix,
                                ori1 + beg1, ori2 - beg2 - 1, size)
    return hits

def orientedBlocks(alignments, seqIndex):
    otherIndex = 1 - seqIndex
    for a in alignments:
        seq1, seq2, blocks = a
        for b in blocks:
            beg1, beg2, size = b
            if b[seqIndex] < 0:
                b = -(beg1 + size), -(beg2 + size), size
            yield a[seqIndex], b[seqIndex], a[otherIndex], b[otherIndex], size

def drawJoins(im, alignments, bpPerPix, seqIndex, rangeDict1, rangeDict2):
    blocks = orientedBlocks(alignments, seqIndex)
    oldSeq1 = ""
    for seq1, beg1, seq2, beg2, size in sorted(blocks):
        isReverse1, ori1 = strandAndOrigin(rangeDict1[seq1], beg1, size)
        isReverse2, ori2 = strandAndOrigin(rangeDict2[seq2], beg2, size)
        end1 = beg1 + size - 1
        end2 = beg2 + size - 1
        if isReverse1:
            beg1 = -(beg1 + 1)
            end1 = -(end1 + 1)
        if isReverse2:
            beg2 = -(beg2 + 1)
            end2 = -(end2 + 1)
        newPix1 = (ori1 + beg1) // bpPerPix
        newPix2 = (ori2 + beg2) // bpPerPix
        if seq1 == oldSeq1:
            lowerPix2 = min(oldPix2, newPix2)
            upperPix2 = max(oldPix2, newPix2)
            midPix1 = (oldPix1 + newPix1) // 2
            if isReverse1:
                midPix1 = (oldPix1 + newPix1 + 1) // 2
                oldPix1, newPix1 = newPix1, oldPix1
            if upperPix2 - lowerPix2 > 1 and oldPix1 <= newPix1 <= oldPix1 + 1:
                if seqIndex == 0:
                    box = midPix1, lowerPix2, midPix1 + 1, upperPix2 + 1
                else:
                    box = lowerPix2, midPix1, upperPix2 + 1, midPix1 + 1
                im.paste("lightgray", box)
        oldPix1 = (ori1 + end1) // bpPerPix
        oldPix2 = (ori2 + end2) // bpPerPix
        oldSeq1 = seq1

def expandedSeqDict(seqDict):
    '''Allow lookup by short sequence names, e.g. chr7 as well as hg19.chr7.'''
    newDict = seqDict.copy()
    for name, x in seqDict.items():
        if "." in name:
            base = name.split(".", 1)[-1]
            if base in newDict:  # an ambiguous case was found:
                return seqDict   # so give up completely
            newDict[base] = x
    return newDict

def annotsFromBedOrAgp(opts, rangeDict, fields):
    seqName = fields[0]
    if seqName not in rangeDict: return
    end = int(fields[2])
    if len(fields) > 7 and fields[4].isalpha():  # agp format, or gap.txt
        if fields[4] in "NU" and fields[5].isdigit():
            beg = end - int(fields[5])  # zero-based coordinate
            if fields[7] == "yes":
                yield 30000, opts.bridged_color, seqName, beg, end, ""
            else:
                yield 20000, opts.unbridged_color, seqName, beg, end, ""
    else:  # BED format
        beg = int(fields[1])
        itemName = fields[3] if len(fields) > 3 and fields[3] != "." else ""
        layer = 900
        color = "#fbf"
        if len(fields) > 4:
            if fields[4] != ".":
                layer = float(fields[4])
            if len(fields) > 5:
                if len(fields) > 8 and fields[8].count(",") == 2:
                    color = "rgb(" + fields[8] + ")"
                else:
                    strand = fields[5]
                    isRev = (rangeDict[seqName][0][3] > 1)
                    if strand == "+" and not isRev or strand == "-" and isRev:
                        color = "#ffe8e8"
                    if strand == "-" and not isRev or strand == "+" and isRev:
                        color = "#e8e8ff"
        yield layer, color, seqName, beg, end, itemName

def annotsFromGpd(opts, rangeDict, fields, geneName):  # xxx split on tabs?
    seqName = fields[1]
    if seqName in rangeDict:
        cdsBeg = int(fields[5])
        cdsEnd = int(fields[6])
        exonBegs = commaSeparatedInts(fields[8])
        exonEnds = commaSeparatedInts(fields[9])
        for beg, end in zip(exonBegs, exonEnds):
            yield 300, opts.exon_color, seqName, beg, end, geneName
            b = max(beg, cdsBeg)
            e = min(end, cdsEnd)
            if b < e: yield 400, opts.cds_color, seqName, b, e, ""

def annotsFromGff(opts, line, seqName):
    fields = line.rstrip().split("\t")
    feature = fields[2]
    beg = int(fields[3]) - 1
    end = int(fields[4])
    if feature == "exon":
        geneName = fields[8]
        if ";" in geneName or "=" in geneName:
            parts = geneName.rstrip(";").split(";")
            attrs = dict(re.split('[= ]', i.strip(), 1) for i in parts)
            if "gene" in attrs:
                geneName = attrs["gene"]  # seems good for NCBI gff
            elif "Name" in attrs:
                geneName = attrs["Name"]
            else:
                geneName = ""
        yield 300, opts.exon_color, seqName, beg, end, geneName
    elif feature == "CDS":
        yield 400, opts.cds_color, seqName, beg, end, ""

def annotsFromRep(rangeDict, seqName, beg, end, strand, repName, repClass):
    simple = "Low_complexity", "Simple_repeat", "Satellite"
    if repClass.startswith(simple):
        yield 200, "#fbf", seqName, beg, end, repName
    elif (strand == "+") != (rangeDict[seqName][0][3] > 1):
        yield 100, "#ffe8e8", seqName, beg, end, repName
    else:
        yield 100, "#e8e8ff", seqName, beg, end, repName

def annotsFromFiles(opts, fileNames, rangeDict):
    isDig = str.isdigit
    for fileName in fileNames:
        for line in openFile(fileName):
            w = line.split()
            n = len(w)
            if n > 10 and w[8] in "+C-" and isDig(w[5]) and isDig(w[6]):
                seq = w[4]                         # RepeatMasker .out
                if seq not in rangeDict: continue  # do this ASAP for speed
                beg = int(w[5]) - 1
                end = int(w[6])
                g = annotsFromRep(rangeDict, seq, beg, end, w[8], w[9], w[10])
            elif n > 11 and w[9] in "+-" and isDig(w[6]) and isDig(w[7]):
                seq = w[5]                         # rmsk.txt
                if seq not in rangeDict: continue
                beg = int(w[6])
                end = int(w[7])
                g = annotsFromRep(rangeDict, seq, beg, end, w[9], w[10], w[11])
            elif n > 8 and w[6] in "+-." and isDig(w[3]) and isDig(w[4]):
                seqName = w[0]
                if seqName not in rangeDict: continue
                g = annotsFromGff(opts, line, seqName)
            elif n > 9 and w[2] in "+-" and isDig(w[4] + w[5] + w[6]):
                geneName = w[12 if n > 12 else 0]  # XXX ???
                g = annotsFromGpd(opts, rangeDict, w, geneName)
            elif n > 10 and w[3] in "+-" and isDig(w[5] + w[6] + w[7]):
                geneName = w[12 if n > 12 else 0]  # XXX ???
                g = annotsFromGpd(opts, rangeDict, w[1:], geneName)
            elif n > 2 and isDig(w[1]) and isDig(w[2]):
                g = annotsFromBedOrAgp(opts, rangeDict, w)
            elif n > 3 and isDig(w[2]) and isDig(w[3]):
                g = annotsFromBedOrAgp(opts, rangeDict, w[1:])
            else:
                continue
            if line[0] == "#": continue
            for i in g:
                layer, color, seqName, beg, end, name = i
                if any(beg < r[2] and end > r[1] for r in rangeDict[seqName]):
                    yield i

def bedBoxes(annots, rangeDict, limit, isTop, bpPerPix):
    beds, textSizes, margin = annots
    cover = [(limit, limit)]
    for layer, color, seqName, bedBeg, bedEnd, name in reversed(beds):
        textWidth, textHeight = textSizes[name]
        for rangeBeg, rangeEnd, isReverseRange, origin in rangeDict[seqName]:
            beg = max(bedBeg, rangeBeg)
            end = min(bedEnd, rangeEnd)
            if beg >= end: continue
            if isReverseRange:
                beg, end = -end, -beg
            if layer <= 10000:
                # include partly-covered pixels
                pixBeg = (origin + beg) // bpPerPix
                pixEnd = div_ceil(origin + end, bpPerPix)
            else:
                # exclude partly-covered pixels
                pixBeg = div_ceil(origin + beg, bpPerPix)
                pixEnd = (origin + end) // bpPerPix
                if pixEnd <= pixBeg: continue
                if bedEnd >= rangeEnd:  # include partly-covered end pixels
                    if isReverseRange:
                        pixBeg = (origin + beg) // bpPerPix
                    else:
                        pixEnd = div_ceil(origin + end, bpPerPix)
            nameBeg = (pixBeg + pixEnd - textHeight) // 2
            nameEnd = nameBeg + textHeight
            n = ""
            if name and all(e <= nameBeg or b >= nameEnd for b, e in cover):
                if textWidth <= margin:
                    cover.append((nameBeg, nameEnd))
                    n = name
            yield layer, color, isTop, pixBeg, pixEnd, n, nameBeg, textWidth

def drawAnnotations(im, boxes, tMargin, bMarginBeg, lMargin, rMarginBeg):
    # xxx use partial transparency for different-color overlaps?
    for layer, color, isTop, beg, end, name, nameBeg, nameLen in boxes:
        if isTop:
            box = beg, tMargin, end, bMarginBeg
        else:
            box = lMargin, beg, rMarginBeg, end
        im.paste(color, box)

def placedLabels(labels, rangePixBegs, rangePixLens, beg, end):
    '''Return axis labels with endpoint & sort-order information.'''
    maxWidth = end - beg
    for i, j, k in zip(labels, rangePixBegs, rangePixLens):
        text, textWidth, textHeight, strandNum = i
        if textWidth > maxWidth:
            continue
        labelBeg = j + (k - textWidth) // 2
        labelEnd = labelBeg + textWidth
        sortKey = textWidth - k
        if labelBeg < beg:
            sortKey += maxWidth * (beg - labelBeg)
            labelBeg = beg
            labelEnd = beg + textWidth
        if labelEnd > end:
            sortKey += maxWidth * (labelEnd - end)
            labelEnd = end
            labelBeg = end - textWidth
        yield sortKey, labelBeg, labelEnd, text, textHeight, strandNum

def nonoverlappingLabels(labels, minPixTweenLabels):
    '''Get a subset of non-overlapping axis labels, greedily.'''
    out = []
    for i in labels:
        beg = i[1] - minPixTweenLabels
        end = i[2] + minPixTweenLabels
        if all(j[2] <= beg or j[1] >= end for j in out):
            out.append(i)
    return out

def axisImage(labels, rangePixBegs, rangePixLens, textRot,
              textAln, font, image_mode, opts):
    '''Make an image of axis labels.'''
    beg = rangePixBegs[0]
    end = rangePixBegs[-1] + rangePixLens[-1]
    margin = max(i[2] for i in labels)
    labels = sorted(placedLabels(labels, rangePixBegs, rangePixLens, beg, end))
    minPixTweenLabels = 0 if textRot else opts.label_space
    labels = nonoverlappingLabels(labels, minPixTweenLabels)
    image_size = (margin, end) if textRot else (end, margin)
    im = Image.new(image_mode, image_size, opts.margin_color)
    draw = ImageDraw.Draw(im)
    for sortKey, labelBeg, labelEnd, text, textHeight, strandNum in labels:
        base = margin - textHeight if textAln else 0
        position = (base, labelBeg) if textRot else (labelBeg, base)
        fill = ("black", opts.forwardcolor, opts.reversecolor)[strandNum]
        draw.text(position, text, font=font, fill=fill)
    return im

def annoTextImage(opts, image_mode, font, margin, length, boxes, isLeftAlign):
    image_size = margin, length
    im = Image.new(image_mode, image_size, opts.margin_color)
    draw = ImageDraw.Draw(im)
    for layer, color, isTop, beg, end, name, nameBeg, nameLen in boxes:
        xPosition = 0 if isLeftAlign else margin - nameLen
        position = xPosition, nameBeg
        draw.text(position, name, font=font, fill="black")
    return im

def rangesPerSeq(sortedRanges):
    for seqName, group in itertools.groupby(sortedRanges, itemgetter(0)):
        yield seqName, sorted(group)

def rangesWithOrigins(sortedRanges, rangePixBegs, rangePixLens, bpPerPix):
    for i, j, k in zip(sortedRanges, rangePixBegs, rangePixLens):
        seqName, rangeBeg, rangeEnd, strandNum = i
        isReverseRange = (strandNum > 1)
        if isReverseRange:
            origin = bpPerPix * (j + k) + rangeBeg
        else:
            origin = bpPerPix * j - rangeBeg
        yield seqName, (rangeBeg, rangeEnd, isReverseRange, origin)

def rangesAndOriginsPerSeq(sortedRanges, rangePixBegs, rangePixLens, bpPerPix):
    a = rangesWithOrigins(sortedRanges, rangePixBegs, rangePixLens, bpPerPix)
    for seqName, group in itertools.groupby(a, itemgetter(0)):
        yield seqName, sorted(i[1] for i in group)

def getFont(opts):
    if opts.fontfile:
        return ImageFont.truetype(opts.fontfile, opts.fontsize)
    fileNames = []
    try:
        x = ["fc-match", "-f%{file}", "arial"]
        p = subprocess.Popen(x, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True)
        out, err = p.communicate()
        fileNames.append(out)
    except OSError as e:
        logging.info("fc-match error: " + str(e))
    fileNames.append("/Library/Fonts/Arial.ttf")  # for Mac
    for i in fileNames:
        try:
            font = ImageFont.truetype(i, opts.fontsize)
            logging.info("font: " + i)
            return font
        except IOError as e:
            logging.info("font load error: " + str(e))
    return ImageFont.load_default()

def sequenceSizesAndNames(seqRanges):
    for seqName, ranges in itertools.groupby(seqRanges, itemgetter(0)):
        size = sum(e - b for n, b, e in ranges)
        yield size, seqName

def biggestSequences(seqRanges, maxNumOfSequences):
    s = sorted(sequenceSizesAndNames(seqRanges), reverse=True)
    if len(s) > maxNumOfSequences:
        logging.warning("too many sequences - discarding the smallest ones")
        s = s[:maxNumOfSequences]
    return set(i[1] for i in s)

def remainingSequenceRanges(seqRanges, alignments, seqIndex):
    remainingSequences = set(i[seqIndex] for i in alignments)
    return [i for i in seqRanges if i[0] in remainingSequences]

def readAnnots(opts, font, textDraw, sortedRanges, totalLength, fileNames):
    rangeDict = expandedSeqDict(dict(rangesPerSeq(sortedRanges)))
    annots = sorted(annotsFromFiles(opts, fileNames, rangeDict))
    names = set(i[5] for i in annots)
    textSizes = dict(sizesPerText(names, font, textDraw))
    maxTextLength = totalLength // 2
    okLengths = [i[0] for i in textSizes.values() if i[0] <= maxTextLength]
    margin = max(okLengths) if okLengths else 0
    return annots, textSizes, margin

def lastDotplot(opts):
    logLevel = logging.INFO if opts.verbose else logging.WARNING
    logging.basicConfig(format="%(filename)s: %(message)s", level=logLevel)

    font = getFont(opts)
    image_mode = 'RGB'
    forward_color = ImageColor.getcolor(opts.forwardcolor, image_mode)
    reverse_color = ImageColor.getcolor(opts.reversecolor, image_mode)
    zipped_colors = zip(forward_color, reverse_color)
    overlap_color = tuple([(i + j) // 2 for i, j in zipped_colors])

    maxGap1, maxGapB1 = twoValuesFromOption(opts.max_gap1, ":")
    maxGap2, maxGapB2 = twoValuesFromOption(opts.max_gap2, ":")

    # logging.info("reading alignments...")
    count = 1
    for alnData in readAlignments(opts.maf, opts, split=True):
        alignments, seqRanges1, coverDict1, seqRanges2, coverDict2 = alnData
        if not alignments: raise RuntimeError("there are no alignments")
        logging.info("cutting...")
        coverDict1 = dict(mergedRangesPerSeq(coverDict1))
        coverDict2 = dict(mergedRangesPerSeq(coverDict2))
        minAlignedBases = min(coveredLength(coverDict1), coveredLength(coverDict2))
        pad = int(opts.pad * minAlignedBases)
        cutRanges1 = list(trimmed(seqRanges1, coverDict1, minAlignedBases,
                                maxGap1, pad, pad))
        cutRanges2 = list(trimmed(seqRanges2, coverDict2, minAlignedBases,
                                maxGap2, pad, pad))

        biggestSeqs1 = biggestSequences(cutRanges1, opts.maxseqs)
        cutRanges1 = [i for i in cutRanges1 if i[0] in biggestSeqs1]
        alignments = [i for i in alignments if i[0] in biggestSeqs1]
        cutRanges2 = remainingSequenceRanges(cutRanges2, alignments, 1)

        biggestSeqs2 = biggestSequences(cutRanges2, opts.maxseqs)
        cutRanges2 = [i for i in cutRanges2 if i[0] in biggestSeqs2]
        alignments = [i for i in alignments if i[1] in biggestSeqs2]
        cutRanges1 = remainingSequenceRanges(cutRanges1, alignments, 0)

        logging.info("reading secondary alignments...")
        alnDataB = readSecondaryAlignments(opts, cutRanges1, cutRanges2)
        alignmentsB, seqRangesB1, coverDictB1, seqRangesB2, coverDictB2 = alnDataB
        logging.info("cutting...")
        coverDictB1 = dict(mergedRangesPerSeq(coverDictB1))
        coverDictB2 = dict(mergedRangesPerSeq(coverDictB2))
        cutRangesB1 = trimmed(seqRangesB1, coverDictB1, minAlignedBases,
                            maxGapB1, 0, 0)
        cutRangesB2 = trimmed(seqRangesB2, coverDictB2, minAlignedBases,
                            maxGapB2, 0, 0)

        logging.info("sorting...")
        sortOut = allSortedRanges(opts, alignments, alignmentsB,
                                cutRanges1, cutRangesB1, cutRanges2, cutRangesB2)
        sortedRanges1, sortedRanges2 = sortOut

        textDraw = None
        if opts.fontsize:
            textDraw = ImageDraw.Draw(Image.new(image_mode, (1, 1)))

        textRot1 = "vertical".startswith(opts.rot1)
        i1 = dataFromRanges(sortedRanges1, font, textDraw, opts.labels1, textRot1)
        rangeSizes1, labelData1, tMargin = i1

        textRot2 = "horizontal".startswith(opts.rot2)
        i2 = dataFromRanges(sortedRanges2, font, textDraw, opts.labels2, textRot2)
        rangeSizes2, labelData2, lMargin = i2

        logging.info("reading annotations...")

        annots1 = readAnnots(opts, font, textDraw, sortedRanges1, opts.height,
                            opts.bed1)
        bMargin = annots1[-1]

        annots2 = readAnnots(opts, font, textDraw, sortedRanges2, opts.width,
                            opts.bed2)
        rMargin = annots2[-1]

        maxPixels1 = opts.width  - lMargin - rMargin
        maxPixels2 = opts.height - tMargin - bMargin
        bpPerPix1 = get_bp_per_pix(rangeSizes1, opts.border_pixels, maxPixels1)
        bpPerPix2 = get_bp_per_pix(rangeSizes2, opts.border_pixels, maxPixels2)
        bpPerPix = max(bpPerPix1, bpPerPix2)
        logging.info("bp per pixel = " + str(bpPerPix))

        p1 = pixelData(rangeSizes1, bpPerPix, opts.border_pixels, lMargin)
        rangePixBegs1, rangePixLens1, rMarginBeg = p1
        width = rMarginBeg + rMargin
        rangeDict1 = dict(rangesAndOriginsPerSeq(sortedRanges1, rangePixBegs1,
                                                rangePixLens1, bpPerPix))

        p2 = pixelData(rangeSizes2, bpPerPix, opts.border_pixels, tMargin)
        rangePixBegs2, rangePixLens2, bMarginBeg = p2
        height = bMarginBeg + bMargin
        rangeDict2 = dict(rangesAndOriginsPerSeq(sortedRanges2, rangePixBegs2,
                                                rangePixLens2, bpPerPix))

        logging.info("width:  " + str(width))
        logging.info("height: " + str(height))

        logging.info("processing alignments...")
        allAlignments = alignments + alignmentsB
        hits = alignmentPixels(width, height, allAlignments, bpPerPix,
                            rangeDict1, rangeDict2)

        rangeDict1 = expandedSeqDict(rangeDict1)
        rangeDict2 = expandedSeqDict(rangeDict2)

        boxes1 = list(bedBoxes(annots1, rangeDict1, rMarginBeg, True, bpPerPix))
        boxes2 = list(bedBoxes(annots2, rangeDict2, bMarginBeg, False, bpPerPix))
        boxes = sorted(itertools.chain(boxes1, boxes2))

        logging.info("drawing...")

        image_size = width, height
        im = Image.new(image_mode, image_size, opts.background_color)

        drawAnnotations(im, boxes, tMargin, bMarginBeg, lMargin, rMarginBeg)

        joinA, joinB = twoValuesFromOption(opts.join, ":")
        if joinA in "13":
            drawJoins(im, alignments, bpPerPix, 0, rangeDict1, rangeDict2)
        if joinB in "13":
            drawJoins(im, alignmentsB, bpPerPix, 0, rangeDict1, rangeDict2)
        if joinA in "23":
            drawJoins(im, alignments, bpPerPix, 1, rangeDict2, rangeDict1)
        if joinB in "23":
            drawJoins(im, alignmentsB, bpPerPix, 1, rangeDict2, rangeDict1)

        for i in range(height):
            for j in range(width):
                store_value = hits[i * width + j]
                xy = j, i
                if   store_value == 1: im.putpixel(xy, forward_color)
                elif store_value == 2: im.putpixel(xy, reverse_color)
                elif store_value == 3: im.putpixel(xy, overlap_color)

        if opts.fontsize != 0:
            axis1 = axisImage(labelData1, rangePixBegs1, rangePixLens1,
                            textRot1, False, font, image_mode, opts)
            if textRot1:
                axis1 = axis1.transpose(Image.ROTATE_90)
            axis2 = axisImage(labelData2, rangePixBegs2, rangePixLens2,
                            textRot2, textRot2, font, image_mode, opts)
            if not textRot2:
                axis2 = axis2.transpose(Image.ROTATE_270)
            im.paste(axis1, (0, 0))
            im.paste(axis2, (0, 0))

            annoImage1 = annoTextImage(opts, image_mode, font, bMargin, width,
                                    boxes1, False)
            annoImage1 = annoImage1.transpose(Image.ROTATE_90)
            annoImage2 = annoTextImage(opts, image_mode, font, rMargin, height,
                                    boxes2, True)
            im.paste(annoImage1, (0, bMarginBeg))
            im.paste(annoImage2, (rMarginBeg, 0))

        for i in rangePixBegs1[1:]:
            box = i - opts.border_pixels, tMargin, i, bMarginBeg
            im.paste(opts.border_color, box)

        for i in rangePixBegs2[1:]:
            box = lMargin, i - opts.border_pixels, rMarginBeg, i
            im.paste(opts.border_color, box)

        im.save(f"{opts.prefix}{count}.png")
        count += 1

if __name__ == "__main__":
    usage = """%prog --help
   or: %prog [options] maf-or-psl-or-tab-alignments
   or: %prog [options] maf-or-psl-or-tab-alignments dotplot.png|gif|..."""
    description = "Draw a dotplot of pair-wise sequence alignments."
    op = optparse.OptionParser(usage=usage, description=description)
    op.add_option("-v", "--verbose", action="count",
                  help="show progress messages & data about the plot")
    # Replace "width" & "height" with a single "length" option?
    op.add_option("-x", "--width", metavar="INT", type="int", default=1000,
                  help="maximum width in pixels (default: %default)")
    op.add_option("-y", "--height", metavar="INT", type="int", default=1000,
                  help="maximum height in pixels (default: %default)")
    op.add_option("-m", "--maxseqs", type="int", default=100, metavar="M",
                  help="maximum number of horizontal or vertical sequences "
                  "(default=%default)")
    op.add_option("-1", "--seq1", metavar="PATTERN", action="append",
                  default=[],
                  help="which sequences to show from the 1st genome")
    op.add_option("-2", "--seq2", metavar="PATTERN", action="append",
                  default=[],
                  help="which sequences to show from the 2nd genome")
    op.add_option("--alignments", metavar="FILE", help="secondary alignments")
    op.add_option("--sort1", default="1", metavar="N",
                  help="genome1 sequence order: 0=input order, 1=name order, "
                  "2=length order, 3=alignment order (default=%default)")
    op.add_option("--sort2", default="1", metavar="N",
                  help="genome2 sequence order: 0=input order, 1=name order, "
                  "2=length order, 3=alignment order (default=%default)")
    op.add_option("--strands1", default="0", metavar="N", help=
                  "genome1 sequence orientation: 0=forward orientation, "
                  "1=alignment orientation (default=%default)")
    op.add_option("--strands2", default="0", metavar="N", help=
                  "genome2 sequence orientation: 0=forward orientation, "
                  "1=alignment orientation (default=%default)")
    op.add_option("--max-gap1", metavar="FRAC", default="1,4", help=
                  "maximum unaligned (end,mid) gap in genome1: "
                  "fraction of aligned length (default=%default)")
    op.add_option("--max-gap2", metavar="FRAC", default="1,4", help=
                  "maximum unaligned (end,mid) gap in genome2: "
                  "fraction of aligned length (default=%default)")
    op.add_option("--pad", metavar="FRAC", type="float", default=0.04, help=
                  "pad length when cutting unaligned gaps: "
                  "fraction of aligned length (default=%default)")
    op.add_option("-j", "--join", default="0", metavar="N", help=
                  "join: 0=nothing, 1=alignments adjacent in genome1, "
                  "2=alignments adjacent in genome2 (default=%default)")
    op.add_option("--border-pixels", metavar="INT", type="int", default=1,
                  help="number of pixels between sequences (default=%default)")
    op.add_option("-a", "--bed1", "--rmsk1", "--genePred1", "--gap1",
                  action="append", default=[], metavar="FILE",
                  help="read genome1 annotations")
    op.add_option("-b", "--bed2", "--rmsk2", "--genePred2", "--gap2",
                  action="append", default=[], metavar="FILE",
                  help="read genome2 annotations")

    og = optparse.OptionGroup(op, "Text options")
    og.add_option("-f", "--fontfile", metavar="FILE",
                  help="TrueType or OpenType font file")
    og.add_option("-s", "--fontsize", metavar="SIZE", type="int", default=14,
                  help="TrueType or OpenType font size (default: %default)")
    og.add_option("--labels1", type="int", default=0, metavar="N", help=
                  "genome1 labels: 0=name, 1=name:length, "
                  "2=name:start:length, 3=name:start-end (default=%default)")
    og.add_option("--labels2", type="int", default=0, metavar="N", help=
                  "genome2 labels: 0=name, 1=name:length, "
                  "2=name:start:length, 3=name:start-end (default=%default)")
    og.add_option("--rot1", metavar="ROT", default="h",
                  help="text rotation for the 1st genome (default=%default)")
    og.add_option("--rot2", metavar="ROT", default="v",
                  help="text rotation for the 2nd genome (default=%default)")
    op.add_option_group(og)

    og = optparse.OptionGroup(op, "Color options")
    og.add_option("-c", "--forwardcolor", metavar="COLOR", default="red",
                  help="color for forward alignments (default: %default)")
    og.add_option("-r", "--reversecolor", metavar="COLOR", default="blue",
                  help="color for reverse alignments (default: %default)")
    og.add_option("--border-color", metavar="COLOR", default="black",
                  help="color for pixels between sequences (default=%default)")
    # --break-color and/or --break-pixels for intra-sequence breaks?
    og.add_option("--margin-color", metavar="COLOR", default="#dcdcdc",
                  help="margin color")
    og.add_option("--exon-color", metavar="COLOR", default="PaleGreen",
                  help="color for exons (default=%default)")
    og.add_option("--cds-color", metavar="COLOR", default="LimeGreen",
                  help="color for protein-coding regions (default=%default)")
    og.add_option("--bridged-color", metavar="COLOR", default="yellow",
                  help="color for bridged gaps (default: %default)")
    og.add_option("--unbridged-color", metavar="COLOR", default="orange",
                  help="color for unbridged gaps (default: %default)")
    op.add_option_group(og)
    opts, args = op.parse_args()
    if len(args) not in (1, 2): op.error("1 or 2 arguments needed")

    opts.background_color = "white"
    opts.label_space = 5     # minimum number of pixels between axis labels

    try: lastDotplot(opts, args)
    except KeyboardInterrupt: pass  # avoid silly error message
    except RuntimeError as e:
        prog = os.path.basename(sys.argv[0])
        sys.exit(prog + ": error: " + str(e))
