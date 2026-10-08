#include <algorithm>
#include <cmath>
#include <cstdint>
#include <vector>

namespace {
constexpr int kTaps = 8;
constexpr int kPhases = 2048;
constexpr int kPhaseStride = kPhases + 1;
constexpr int kCutoffs = 512;

const std::vector<float> &kernel_table() {
    static const std::vector<float> table = [] {
        std::vector<float> result(kPhaseStride * kCutoffs * kTaps);
        for (int c = 0; c < kCutoffs; ++c) {
            const double cutoff = static_cast<double>(c) / (kCutoffs - 1);
            for (int p = 0; p <= kPhases; ++p) {
                const double phase = static_cast<double>(p) / kPhases;
                double weights[kTaps];
                double sum = 0.0;
                for (int t = 0; t < kTaps; ++t) {
                    const double distance = phase - (t - 3);
                    const double scaled = distance * cutoff;
                    const double sinc = std::abs(scaled) < 1e-12
                        ? 1.0
                        : std::sin(M_PI * scaled) / (M_PI * scaled);
                    const double window = std::abs(distance) < 4.0
                        ? 0.5 + 0.5 * std::cos(M_PI * distance / 4.0)
                        : 0.0;
                    weights[t] = cutoff * sinc * window;
                    sum += weights[t];
                }
                const std::size_t base =
                    (static_cast<std::size_t>(c) * kPhaseStride + p) * kTaps;
                const double divisor = std::abs(sum) < 1e-12 ? 1.0 : sum;
                for (int t = 0; t < kTaps; ++t)
                    result[base + t] = static_cast<float>(weights[t] / divisor);
            }
        }
        return result;
    }();
    return table;
}
} // namespace

extern "C" void trackscript_sinc_interpolate(
    const float *data, int sample_count, int channels,
    const double *positions, const double *steps, const double *raw_positions,
    int frames, int loop_start, int loop_end, float *output) {
    const auto &table = kernel_table();
    const int loop_length = loop_end - loop_start;

    for (int frame = 0; frame < frames; ++frame) {
        const double position = positions[frame];
        const double floor_position = std::floor(position);
        const int64_t center = static_cast<int64_t>(floor_position);
        const double fraction = position - floor_position;
        const double phase_position = fraction * kPhases;
        const int phase0 = std::min(kPhases - 1,
            static_cast<int>(phase_position));
        const int phase1 = phase0 + 1;
        const double phase_mix = phase_position - phase0;
        const double cutoff = std::min(1.0, 1.0 / std::max(steps[frame], 1e-12));
        const double cutoff_position = cutoff * (kCutoffs - 1);
        const int cutoff0 = std::min(kCutoffs - 1,
            static_cast<int>(cutoff_position));
        const int cutoff1 = std::min(kCutoffs - 1, cutoff0 + 1);
        const double cutoff_mix = cutoff_position - cutoff0;
        const float *w00 = table.data() +
            (static_cast<std::size_t>(cutoff0) * kPhaseStride + phase0) * kTaps;
        const float *w01 = table.data() +
            (static_cast<std::size_t>(cutoff0) * kPhaseStride + phase1) * kTaps;
        const float *w10 = table.data() +
            (static_cast<std::size_t>(cutoff1) * kPhaseStride + phase0) * kTaps;
        const float *w11 = table.data() +
            (static_cast<std::size_t>(cutoff1) * kPhaseStride + phase1) * kTaps;
        const bool already_looped = raw_positions[frame] >= loop_end;

        for (int channel = 0; channel < channels; ++channel) {
            double value = 0.0;
            for (int tap = 0; tap < kTaps; ++tap) {
                const double top = w00[tap] + phase_mix * (w01[tap] - w00[tap]);
                const double bottom = w10[tap] + phase_mix * (w11[tap] - w10[tap]);
                const double weight = top + cutoff_mix * (bottom - top);
                int64_t index = center + tap - 3;
                if (loop_length > 0 &&
                    (index >= loop_end || (already_looped && index < loop_start))) {
                    const int64_t relative = (index - loop_start) % loop_length;
                    index = loop_start + (relative < 0 ? relative + loop_length : relative);
                }
                index = std::clamp<int64_t>(index, 0, sample_count - 1);
                value += static_cast<double>(data[index * channels + channel]) * weight;
            }
            output[frame * channels + channel] = static_cast<float>(value);
        }
    }
}
