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
// build (MSVC x64):
//   cl /nologo /O2 /EHsc /std:c++17 /I lercv1 lerc1enc.cpp lercv1\Lerc1Image.cpp /Fe:lerc1enc.exe

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include "Lerc1Image.h"

USING_NAMESPACE_LERC1

int main(int argc, char **argv)
{
    if (argc < 5)
    {
        fprintf(stderr, "usage: lerc1enc <in.raw> <cols> <rows> <out.lerc1> "
                        "[maxZErr=0.01] [nodata=-32768]\n");
        return 2;
    }
    const char *inPath = argv[1];
    const int cols = atoi(argv[2]);
    const int rows = atoi(argv[3]);
    const char *outPath = argv[4];
    const double maxZErr = (argc > 5) ? atof(argv[5]) : 0.01;
    const float nodata = (argc > 6) ? (float)atof(argv[6]) : -32768.0f;

    if (cols <= 0 || rows <= 0)
    {
        fprintf(stderr, "bad dimensions\n");
        return 2;
    }

    std::vector<float> buf((size_t)cols * rows);
    FILE *f = fopen(inPath, "rb");
    if (!f)
    {
        perror("open input");
        return 1;
    }
    const size_t nread = fread(buf.data(), sizeof(float), buf.size(), f);
    fclose(f);
    if (nread != buf.size())
    {
        fprintf(stderr, "input size mismatch: got %zu floats, expected %zu\n",
                nread, buf.size());
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

    // upper bound: raw floats + headers/mask overhead
    std::vector<unsigned char> out((size_t)cols * rows * 4 + 65536);
    unsigned char *p = out.data();
    if (!img.write(&p, maxZErr, false))
    {
        fprintf(stderr, "Lerc1 encode failed\n");
        return 1;
    }
    const size_t nbytes = (size_t)(p - out.data());

    FILE *g = fopen(outPath, "wb");
    if (!g)
    {
        perror("open output");
        return 1;
    }
    const size_t nwritten = fwrite(out.data(), 1, nbytes, g);
    fclose(g);
    if (nwritten != nbytes)
    {
        fprintf(stderr, "short write\n");
        return 1;
    }
    return 0;
}
