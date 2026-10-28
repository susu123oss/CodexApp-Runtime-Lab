#include <mach-o/dyld.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

extern "C" int node_start(int argc, char *argv[]);

static std::string dirnameOf(const std::string &path) {
    const auto pos = path.find_last_of('/');
    if (pos == std::string::npos) return ".";
    if (pos == 0) return "/";
    return path.substr(0, pos);
}

static void prependNodeOption(const std::string &option) {
    if (option.empty()) return;
    const char *old = std::getenv("NODE_OPTIONS");
    std::string current = old && *old ? std::string(old) : std::string();
    if (current.find(option) != std::string::npos) return;
    std::string value = option;
    if (!current.empty()) value += " " + current;
    ::setenv("NODE_OPTIONS", value.c_str(), 1);
}

static std::string basenameOf(const std::string &path) {
    const auto pos = path.find_last_of('/');
    return pos == std::string::npos ? path : path.substr(pos + 1);
}

static std::string executablePath() {
    uint32_t size = 0;
    _NSGetExecutablePath(nullptr, &size);
    std::vector<char> buffer(size + 1, 0);
    if (_NSGetExecutablePath(buffer.data(), &size) != 0) return {};
    return std::string(buffer.data());
}

static bool ensureDirectory(const std::string &path) {
    if (path.empty()) return false;
    std::string current;
    if (path.front() == '/') current = "/";
    size_t start = path.front() == '/' ? 1 : 0;
    while (start <= path.size()) {
        size_t slash = path.find('/', start);
        std::string part = path.substr(start, slash == std::string::npos ? std::string::npos : slash - start);
        if (!part.empty()) {
            if (!current.empty() && current.back() != '/') current += '/';
            current += part;
            if (::mkdir(current.c_str(), 0755) != 0 && errno != EEXIST) return false;
        }
        if (slash == std::string::npos) break;
        start = slash + 1;
    }
    return true;
}

static void setDefault(const char *key, const std::string &value) {
    const char *old = std::getenv(key);
    if ((!old || !*old) && !value.empty()) ::setenv(key, value.c_str(), 1);
}

static void prepareWritableNodeEnvironment(const std::string &runtimeBin) {
    const char *homeRaw = std::getenv("HOME");
    if (!homeRaw || !*homeRaw) return;
    std::string base = std::string(homeRaw) + "/Library/Application Support/CodexApp/Node";
    std::string prefix = base + "/prefix";
    std::string cache = base + "/cache";
    std::string corepack = base + "/corepack";
    ensureDirectory(prefix + "/bin");
    ensureDirectory(prefix + "/lib/node_modules");
    ensureDirectory(cache);
    ensureDirectory(corepack);
    setDefault("NPM_CONFIG_PREFIX", prefix);
    setDefault("NPM_CONFIG_CACHE", cache);
    setDefault("COREPACK_HOME", corepack);
    setDefault("NODE_PATH", prefix + "/lib/node_modules");
    const std::string runtimeRoot = dirnameOf(runtimeBin);
    setDefault("npm_config_nodedir", runtimeRoot + "/devkit/node");
    setDefault("npm_config_node_gyp", runtimeRoot + "/lib/codex/node-gyp-ios.js");
    setDefault("npm_config_build_from_source", "true");
    setDefault("npm_config_arch", "arm64");
    setDefault("npm_config_target_arch", "arm64");
    prependNodeOption("--require=" + runtimeRoot + "/lib/codex/ios-bootstrap.js");
    const char *shell = std::getenv("SHELL");
    if (shell && *shell) setDefault("npm_config_script_shell", shell);
    const char *oldPath = std::getenv("PATH");
    std::string userBin = prefix + "/bin";
    std::string path = runtimeBin + ":" + userBin;
    if (oldPath && *oldPath) path += ":" + std::string(oldPath);
    ::setenv("PATH", path.c_str(), 1);
}

static std::string cliScriptFor(const std::string &name, const std::string &root) {
    if (name == "npm") return root + "/lib/node_modules/npm/bin/npm-cli.js";
    if (name == "npx") return root + "/lib/node_modules/npm/bin/npx-cli.js";
    if (name == "corepack") return root + "/lib/node_modules/corepack/dist/corepack.js";
    if (name == "pnpm") return root + "/lib/node_modules/corepack/dist/pnpm.js";
    if (name == "pnpx") return root + "/lib/node_modules/corepack/dist/pnpx.js";
    if (name == "yarn") return root + "/lib/node_modules/corepack/dist/yarn.js";
    if (name == "yarnpkg") return root + "/lib/node_modules/corepack/dist/yarnpkg.js";
    if (name == "node-gyp") return root + "/lib/codex/node-gyp-ios.js";
    return {};
}

static std::vector<std::string> normalizedNodeArguments(int argc, char **argv,
                                                        const std::string &exe,
                                                        const std::string &script) {
    std::vector<std::string> owned;
    owned.reserve(static_cast<size_t>(argc) + (script.empty() ? 2 : 3));
    owned.push_back(exe.empty() ? std::string("node") : exe);
    // nodejs-mobile builds iOS V8 in JITless mode. V8 otherwise starts with
    // expose_wasm=true and immediately disables it again during Initialize(),
    // printing a warning on every JS invocation. Make the final state explicit
    // before V8 initialization so node/npm/npx/JS commands stay quiet.
    owned.push_back("--no-expose-wasm");
    if (!script.empty()) owned.push_back(script);
    for (int i = 1; i < argc; ++i) owned.emplace_back(argv[i] ? argv[i] : "");
    return owned;
}

int main(int argc, char **argv) {
    const std::string exe = executablePath();
    prepareWritableNodeEnvironment(dirnameOf(exe));
    const std::string root = dirnameOf(dirnameOf(exe));
    const std::string invoked = argc > 0 && argv[0] ? basenameOf(argv[0]) : "node";
    const std::string script = cliScriptFor(invoked, root);
    std::vector<std::string> owned=normalizedNodeArguments(argc, argv, exe, script);
    std::vector<char *> forwarded;
    forwarded.reserve(owned.size() + 1);
    for (std::string &value : owned) forwarded.push_back(value.data());
    forwarded.push_back(nullptr);
    return node_start(static_cast<int>(owned.size()), forwarded.data());
}
