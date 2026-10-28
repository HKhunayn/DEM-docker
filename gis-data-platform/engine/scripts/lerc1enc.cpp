// lerc1enc — encode raw float32 raster (row-major, little-endian) as a
// Lerc1 "CntZImage" blob — the format ArcGIS elevation clients expect
// (ArcGIS Maps SDK for JS/Qt/.NET, ArcGIS Pro, ArcGIS Earth all decode Lerc1).
//
// Encoder source: GDAL's frmts/mrf/LERCV1 (Esri Lerc1, Apache-2.0) — kept in
// scripts/lercv1/ so the whole engine builds offline.
//
// usage: lerc1enc <in.raw> <cols> <rows> <out.lerc1> [maxZErr=0.01] [nodata=-32768]
//   maxZErr = 0  -> lossless (raw float blocks)
//   maxZErr  > 0 -> lossy with error bound in meters (quantized + bit-stuffed)
//
// Exit status: 0 ok, 2 usage/argument error, 1 I/O or encode error (message on
// stderr). On failure no partial output file is left behind.
//
// build (MSVC x64):  build_lerc1enc.bat
//   cl /nologo /O2 /EHsc /std:c++17 /I lercv1 lerc1enc.cpp lercv1\Lerc1Image.cpp /Fe:lerc1enc.exe
// build (g++/clang): build_lerc1enc.sh

#include <cerrno>
#include <climits>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>
#include <sys/stat.h>
#include <vector>

#include "Lerc1Image.h"

USING_NAMESPACE_LERC1

namespace
{

// The encoder's own exact size computation,
// Lerc1Image::computeNumBytesNeededToWrite(), is protected and Lerc1Image is
// `final`, and the vendored lercv1/ sources are kept unmodified. Explicit
// template instantiation is exempt from access checking ([temp.explicit]), so
// instantiating SizeFnTag with the member pointer hands it to us through a
// friend function — standard C++17, works with g++, clang and MSVC.
auto lerc1SizeFn();

template <auto Fn>
struct SizeFnTag
{
    friend auto lerc1SizeFn()
    {
        return Fn;
    }
};

template struct SizeFnTag<&Lerc1Image::computeNumBytesNeededToWrite>;

// Info is deduced (never named), so the protected nested type stays usable.
template <class Img, class Info>
unsigned int callSizeFn(unsigned int (Img::*fn)(double, bool, Info *) const,
                        const Img &img, double maxZError)
{
    Info info;
    return (img.*fn)(maxZError, false, &info);
}

// Exact number of bytes img.write(&p, maxZError, false) emits; 0 on failure.
unsigned int bytesNeeded(const Lerc1Image &img, double maxZError)
{
    return callSizeFn(lerc1SizeFn(), img, maxZError);
}

bool parseInt(const char *s, int &out)
{
    errno = 0;
    char *end = nullptr;
    const long v = strtol(s, &end, 10);
    if (errno != 0 || end == s || *end != '\0' || v <= 0 || v > INT_MAX)
        return false;
    out = (int)v;
    return true;
}

// NaN is always rejected; +-inf only when allowInf (e.g. nodata=-inf).
bool parseDouble(const char *s, double &out, bool allowInf = false)
{
    errno = 0;
    char *end = nullptr;
    const double v = strtod(s, &end);
    if (end == s || *end != '\0' || std::isnan(v) ||
        (!allowInf && (errno == ERANGE || std::isinf(v))))
        return false;
    out = v;
    return true;
}

// Drop a partial output, but never unlink devices/pipes (e.g. /dev/stdout).
void removeIfRegular(const char *path)
{
    struct stat st;
    if (stat(path, &st) == 0 && (st.st_mode & S_IFMT) == S_IFREG)
        remove(path);
}

}  // namespace

int main(int argc, char **argv)
{
    if (argc < 5 || argc > 7)
    {
        fprintf(stderr, "usage: lerc1enc <in.raw> <cols> <rows> <out.lerc1> "
                        "[maxZErr=0.01] [nodata=-32768]\n");
        return 2;
    }
    const char *inPath = argv[1];
    const char *outPath = argv[4];
    int cols = 0, rows = 0;
    double maxZErr = 0.01, nodataD = -32768.0;
    if (!parseInt(argv[2], cols) || !parseInt(argv[3], rows))
    {
        fprintf(stderr, "lerc1enc: bad dimensions '%s' x '%s'\n", argv[2], argv[3]);
        return 2;
    }
    if (argc > 5 && (!parseDouble(argv[5], maxZErr) || maxZErr < 0))
    {
        fprintf(stderr, "lerc1enc: bad maxZErr '%s' (finite number >= 0)\n", argv[5]);
        return 2;
    }
    if (argc > 6 && !parseDouble(argv[6], nodataD, true))
    {
        fprintf(stderr, "lerc1enc: bad nodata '%s'\n", argv[6]);
        return 2;
    }
    const float nodata = (float)nodataD;

    const size_t npix = (size_t)cols * (size_t)rows;
    // Lerc1Image indexes pixels with int
    if (npix / (size_t)cols != (size_t)rows || npix > (size_t)INT_MAX)
    {
        fprintf(stderr, "lerc1enc: raster too large (%d x %d)\n", cols, rows);
        return 2;
    }

    try
    {
        std::vector<float> buf(npix);
        FILE *f = fopen(inPath, "rb");
        if (!f)
        {
            fprintf(stderr, "lerc1enc: cannot open input '%s': %s\n", inPath, strerror(errno));
            return 1;
        }
        const size_t nread = fread(buf.data(), sizeof(float), buf.size(), f);
        const bool readErr = ferror(f) != 0;
        // input must be exactly cols*rows floats — no trailing bytes
        const bool extra = (fgetc(f) != EOF);
        fclose(f);
        if (readErr || nread != buf.size() || extra)
        {
            fprintf(stderr, "lerc1enc: input size mismatch: got %zu%s floats, expected %zu\n",
                    nread, extra ? "+" : "", buf.size());
            return 1;
        }

        Lerc1Image img;
        img.resize(cols, rows);
        for (int r = 0; r < rows; r++)
        {
            for (int c = 0; c < cols; c++)
            {
                const float v = buf[(size_t)r * cols + c];
                const bool valid = (v == v) && v > nodata + 1.0f;  // not NaN, not nodata
                img(r, c) = valid ? v : 0.0f;
                img.SetMask(r, c, valid);
            }
        }

        // Size the buffer from the encoder's own (exact) size computation
        // instead of guessing; keep a little slack and verify afterwards.
        const unsigned int need = bytesNeeded(img, maxZErr);
        if (need == 0)
        {
            fprintf(stderr, "lerc1enc: Lerc1 size computation failed\n");
            return 1;
        }
        std::vector<unsigned char> out((size_t)need + 64);
        unsigned char *p = out.data();
        if (!img.write(&p, maxZErr, false))
        {
            fprintf(stderr, "lerc1enc: Lerc1 encode failed\n");
            return 1;
        }
        const size_t nbytes = (size_t)(p - out.data());
        if (nbytes != (size_t)need)
        {
            fprintf(stderr, "lerc1enc: encoder wrote %zu bytes, expected %u\n", nbytes, need);
            return 1;
        }

        FILE *g = fopen(outPath, "wb");
        if (!g)
        {
            fprintf(stderr, "lerc1enc: cannot open output '%s': %s\n", outPath, strerror(errno));
            return 1;
        }
        const size_t nwritten = fwrite(out.data(), 1, nbytes, g);
        const bool writeErr = ferror(g) != 0;
        const bool closeErr = fclose(g) != 0;
        if (writeErr || closeErr || nwritten != nbytes)
        {
            fprintf(stderr, "lerc1enc: write to '%s' failed (%zu of %zu bytes)\n",
                    outPath, nwritten, nbytes);
            removeIfRegular(outPath);
            return 1;
        }
    }
    catch (const std::bad_alloc &)
    {
        fprintf(stderr, "lerc1enc: out of memory\n");
        return 1;
    }
    catch (const std::exception &e)
    {
        fprintf(stderr, "lerc1enc: %s\n", e.what());
        return 1;
    }
    return 0;
}
