// Standalone Rime Octagram (.gram) Hybrid Merger & Inspector
// Build: c++ -O3 -std=c++17 others/script/gram/merge_gram.cc -Iothers/script/gram -o merge_gram
// Requires darts.h (Darts-clone v0.32, BSD license) in include path.
//
// Key encoding follows upstream lotem/librime-octagram (src/gram_encoding.cc):
//   - U+0000            -> 0xE0
//   - U+0001..U+007F    -> 1 byte (ASCII)
//   - U+4000..U+9FFF    -> 2 bytes ((u >> 8) + 0x40, u & 0xFF), or (0xE1, (u >> 8) + 0x40) if low byte == 0
//   - Other codepoints  -> (0xE0 | n) followed by n 7-bit chunks (0x80 | chunk), left-aligned to bit 31 (u << 3)
#include <algorithm>
#include <cerrno>
#include <climits>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>
#include <utility>
#include <vector>
#include "darts.h"

struct GramHeader {
  char format[32];
  uint32_t db_checksum;
  uint32_t double_array_size;
  int32_t array_offset_rel;
};

static inline void strip_cr(std::string& s) {
  while (!s.empty() && (s.back() == '\r' || s.back() == '\n')) {
    s.pop_back();
  }
}

static inline bool parse_int_safe(const std::string& s, int& out) {
  if (s.empty()) return false;
  char* endptr = nullptr;
  errno = 0;
  long v = std::strtol(s.c_str(), &endptr, 10);
  if (errno != 0 || endptr == s.c_str()) return false;
  while (*endptr == ' ' || *endptr == '\t' || *endptr == '\r') ++endptr;
  if (*endptr != '\0' || v < INT_MIN || v > INT_MAX) return false;
  out = static_cast<int>(v);
  return true;
}

static inline bool next_utf8_cp(const unsigned char*& p, const unsigned char* end, uint32_t& u) {
  if (p >= end) return false;
  unsigned char b0 = *p;
  if (b0 < 0x80) {
    u = b0;
    ++p;
    return true;
  }
  if ((b0 & 0xE0) == 0xC0) {
    if (p + 1 >= end || (p[1] & 0xC0) != 0x80) {
      ++p;
      return false;
    }
    u = ((uint32_t(b0 & 0x1F)) << 6) | uint32_t(p[1] & 0x3F);
    p += 2;
    return u >= 0x80;
  }
  if ((b0 & 0xF0) == 0xE0) {
    if (p + 2 >= end || (p[1] & 0xC0) != 0x80 || (p[2] & 0xC0) != 0x80) {
      ++p;
      return false;
    }
    u = ((uint32_t(b0 & 0x0F)) << 12) | ((uint32_t(p[1] & 0x3F)) << 6) | uint32_t(p[2] & 0x3F);
    p += 3;
    return u >= 0x800 && !(u >= 0xD800 && u <= 0xDFFF);
  }
  if ((b0 & 0xF8) == 0xF0) {
    if (p + 3 >= end || (p[1] & 0xC0) != 0x80 || (p[2] & 0xC0) != 0x80 || (p[3] & 0xC0) != 0x80) {
      ++p;
      return false;
    }
    u = ((uint32_t(b0 & 0x07)) << 18) | ((uint32_t(p[1] & 0x3F)) << 12) |
        ((uint32_t(p[2] & 0x3F)) << 6) | uint32_t(p[3] & 0x3F);
    p += 4;
    return u >= 0x10000 && u <= 0x10FFFF;
  }
  ++p;
  return false;
}

static std::string encode_gram_key(const std::string& utf8) {
  std::string out;
  out.reserve(utf8.size());
  const unsigned char* p = reinterpret_cast<const unsigned char*>(utf8.data());
  const unsigned char* end = p + utf8.size();
  while (p < end) {
    uint32_t u = 0;
    if (!next_utf8_cp(p, end, u)) continue;

    if (u < 0x80) {
      out.push_back(u == 0 ? static_cast<char>(0xE0) : static_cast<char>(u));
    } else if (u >= 0x4000 && u < 0xA000) {
      if ((u & 0xFF) == 0) {
        out.push_back(static_cast<char>(0xE1));
        out.push_back(static_cast<char>((u >> 8) + 0x40));
      } else {
        out.push_back(static_cast<char>((u >> 8) + 0x40));
        out.push_back(static_cast<char>(u & 0xFF));
      }
    } else {
      int bits = 32;
      uint32_t tmp = u;
      while (bits > 0 && (tmp & 0xFE000000u) == 0) {
        bits -= 7;
        tmp <<= 7;
      }
      int bytes_to_encode = (bits + 6) / 7;
      out.push_back(static_cast<char>(0xE0 | bytes_to_encode));
      while (bytes_to_encode > 0) {
        --bytes_to_encode;
        out.push_back(static_cast<char>(((tmp >> 25) & 0x7F) | 0x80));
        tmp <<= 7;
      }
    }
  }
  return out;
}

static bool has_gram_magic(const std::string& path) {
  std::ifstream fin(path, std::ios::binary);
  if (!fin) return false;
  char magic[14] = {};
  if (!fin.read(magic, sizeof(magic))) return false;
  return std::strncmp(magic, "Rime::Grammar/", 14) == 0;
}

static bool load_gram_da(
    const std::string& path,
    GramHeader& hdr,
    std::vector<char>& buf,
    Darts::DoubleArray& da) {
  std::ifstream fin(path, std::ios::binary | std::ios::ate);
  if (!fin) {
    std::cerr << "Error: cannot open .gram file: " << path << "\n";
    return false;
  }
  std::streamoff end_pos = fin.tellg();
  if (end_pos < static_cast<std::streamoff>(sizeof(GramHeader))) {
    std::cerr << "Error: .gram file too small: " << path << "\n";
    return false;
  }
  size_t file_size = static_cast<size_t>(end_pos);
  fin.seekg(0);
  if (!fin.read(reinterpret_cast<char*>(&hdr), sizeof(hdr))) {
    std::cerr << "Error: failed reading .gram header: " << path << "\n";
    return false;
  }
  if (std::strncmp(hdr.format, "Rime::Grammar/", 14) != 0) {
    std::cerr << "Error: invalid .gram magic header in " << path << "\n";
    return false;
  }
  int64_t array_start_s = static_cast<int64_t>(offsetof(GramHeader, array_offset_rel)) +
                          static_cast<int64_t>(hdr.array_offset_rel);
  if (array_start_s < static_cast<int64_t>(sizeof(GramHeader)) ||
      static_cast<uint64_t>(array_start_s) > file_size) {
    std::cerr << "Error: invalid array_offset_rel in " << path << "\n";
    return false;
  }
  size_t array_start = static_cast<size_t>(array_start_s);
  size_t array_bytes = static_cast<size_t>(hdr.double_array_size) * 4ULL;
  if (hdr.double_array_size == 0 || array_bytes > file_size - array_start) {
    std::cerr << "Error: truncated double-array payload in " << path << "\n";
    return false;
  }
  buf.resize(array_bytes);
  fin.seekg(static_cast<std::streamoff>(array_start));
  if (!fin.read(buf.data(), static_cast<std::streamsize>(array_bytes))) {
    std::cerr << "Error: failed reading double-array payload from " << path << "\n";
    return false;
  }
  da.set_array(buf.data(), hdr.double_array_size);
  return true;
}

static size_t traverse_da_entries(
    const Darts::DoubleArray& da,
    int min_keep,
    std::vector<std::pair<std::string, int>>& out) {
  struct Frame {
    size_t node_pos;
    int next_byte;
  };
  size_t added = 0;
  std::string cur_key;
  cur_key.reserve(32);
  std::vector<Frame> st = {{0, 1}};
  while (!st.empty()) {
    Frame& top = st.back();
    bool moved = false;
    while (top.next_byte < 256) {
      char ch = static_cast<char>(top.next_byte++);
      size_t npos = top.node_pos, kpos = 0;
      int res = da.traverse(&ch, npos, kpos, 1);
      if (res != -2) {
        cur_key.push_back(ch);
        if (res >= min_keep && res >= 0) {
          out.emplace_back(cur_key, res);
          ++added;
        }
        st.push_back({npos, 1});
        moved = true;
        break;
      }
    }
    if (!moved) {
      st.pop_back();
      if (!cur_key.empty()) cur_key.pop_back();
    }
  }
  return added;
}

static bool write_gram_file(
    const std::string& out_path,
    const std::vector<std::pair<std::string, int>>& sorted_kv) {
  if (sorted_kv.empty()) {
    std::cerr << "Error: cannot build .gram from 0 valid entries\n";
    return false;
  }
  std::vector<const char*> keys(sorted_kv.size());
  std::vector<size_t> lengths(sorted_kv.size());
  std::vector<int> vals(sorted_kv.size());
  for (size_t i = 0; i < sorted_kv.size(); ++i) {
    keys[i] = sorted_kv[i].first.c_str();
    lengths[i] = sorted_kv[i].first.size();
    vals[i] = sorted_kv[i].second;
  }
  Darts::DoubleArray new_da;
  if (new_da.build(sorted_kv.size(), keys.data(), lengths.data(), vals.data()) != 0) {
    std::cerr << "Error building Darts DoubleArray!\n";
    return false;
  }
  GramHeader out_hdr{};
  std::memset(&out_hdr, 0, sizeof(out_hdr));
  std::strncpy(out_hdr.format, "Rime::Grammar/1.0", 31);
  out_hdr.db_checksum = 0;
  out_hdr.double_array_size = static_cast<uint32_t>(new_da.size());
  out_hdr.array_offset_rel = 4;

  std::ofstream fout(out_path, std::ios::binary);
  if (!fout) {
    std::cerr << "Error: cannot open output .gram file: " << out_path << "\n";
    return false;
  }
  fout.write(reinterpret_cast<const char*>(&out_hdr), sizeof(out_hdr));
  fout.write(reinterpret_cast<const char*>(new_da.array()), new_da.total_size());
  if (!fout) {
    std::cerr << "Error: failed writing output .gram file: " << out_path << "\n";
    return false;
  }
  fout.close();
  double mib = (sizeof(out_hdr) + new_da.total_size()) / (1024.0 * 1024.0);
  std::cerr << "Built " << out_path << " | keys=" << sorted_kv.size()
            << " | units=" << new_da.size()
            << " | size=" << mib << " MiB\n";
  return true;
}

int main(int argc, char** argv) {
  if (argc < 3) {
    std::cerr << "Usage:\n"
              << "  merge_gram merge <base.gram> <overlay.gram_or_tsv> <min_keep_val> <out.gram>\n"
              << "  merge_gram query <file.gram> <utf8_key1|- (stdin)> [utf8_key2 ...]\n";
    return 1;
  }
  std::string cmd = argv[1];

  if (cmd == "query" && argc >= 4) {
    GramHeader hdr{};
    std::vector<char> buf;
    Darts::DoubleArray da;
    if (!load_gram_da(argv[2], hdr, buf, da)) return 1;
    auto query_one = [&](const std::string& key) {
      std::string enc = encode_gram_key(key);
      int v = enc.empty() ? -1 : da.exactMatchSearch<int>(enc.c_str(), enc.size());
      std::cout << key << "\t" << v << "\n";
    };
    if (argc == 4 && std::string(argv[3]) == "-") {
      std::string line;
      while (std::getline(std::cin, line)) {
        strip_cr(line);
        if (!line.empty()) query_one(line);
      }
    } else {
      for (int i = 3; i < argc; ++i) {
        query_one(argv[i]);
      }
    }
    return 0;
  }

  if (cmd == "merge" && argc >= 6) {
    std::string base_path = argv[2];
    std::string overlay_path = argv[3];
    int min_keep = 0;
    if (!parse_int_safe(argv[4], min_keep)) {
      std::cerr << "Error: invalid integer min_keep_val: " << argv[4] << "\n";
      return 1;
    }
    std::string out_path = argv[5];

    std::vector<std::pair<std::string, int>> merged;
    size_t base_kept = 0;
    {
      GramHeader hdr{};
      std::vector<char> buf;
      Darts::DoubleArray da;
      if (!load_gram_da(base_path, hdr, buf, da)) return 1;
      size_t est_keys = std::min<size_t>(
          60000000ULL,
          static_cast<size_t>(hdr.double_array_size) / 2ULL + 1200000ULL);
      merged.reserve(est_keys);
      base_kept = traverse_da_entries(da, min_keep, merged);
      std::cerr << "Base kept (>=" << min_keep << "): " << base_kept << "\n";
    }

    size_t overlay_added = 0;
    if (has_gram_magic(overlay_path)) {
      GramHeader hdr{};
      std::vector<char> buf;
      Darts::DoubleArray da;
      if (!load_gram_da(overlay_path, hdr, buf, da)) return 1;
      overlay_added = traverse_da_entries(da, 1, merged);
      std::cerr << "Overlay (.gram) loaded: " << overlay_added << "\n";
    } else {
      std::ifstream fin(overlay_path);
      if (!fin) {
        std::cerr << "Error: cannot open overlay file: " << overlay_path << "\n";
        return 1;
      }
      std::string line;
      while (std::getline(fin, line)) {
        strip_cr(line);
        if (line.empty() || line[0] == '#') continue;
        size_t tab = line.find('\t');
        if (tab == std::string::npos) continue;
        std::string k = line.substr(0, tab);
        int v = 0;
        if (!parse_int_safe(line.substr(tab + 1), v)) continue;
        if (k.empty() || v <= 0) continue;
        std::string enc = encode_gram_key(k);
        if (enc.empty() || enc.find('\0') != std::string::npos) continue;
        merged.emplace_back(std::move(enc), v);
        ++overlay_added;
      }
      std::cerr << "Overlay (.tsv) loaded: " << overlay_added << "\n";
    }

    std::sort(merged.begin(), merged.end(), [](const auto& a, const auto& b) {
      if (a.first != b.first) return a.first < b.first;
      return a.second > b.second;
    });
    size_t mw = 0;
    for (size_t r = 0; r < merged.size(); ++r) {
      if (mw == 0 || merged[r].first != merged[mw - 1].first) {
        if (mw != r) merged[mw] = std::move(merged[r]);
        ++mw;
      }
    }
    merged.resize(mw);
    std::cerr << "Merged unique keys: " << merged.size() << "\n";
    return write_gram_file(out_path, merged) ? 0 : 1;
  }

  return 1;
}
