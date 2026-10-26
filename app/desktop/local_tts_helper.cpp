// Project-owned Windows file/CLI adapter for unchanged sherpa-onnx 2.0.0 C API.
// No microphone, device playback, network, downloads, or system configuration.
#define UNICODE
#define _UNICODE
#define NOMINMAX
#include <windows.h>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>
#include <stdexcept>
#include <iostream>
#include <memory>
#include <functional>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <algorithm>
#include "sherpa-onnx/c-api/c-api.h"

namespace fs=std::filesystem;
using Clock=std::chrono::steady_clock;
constexpr size_t kMaxTextBytes=262144;
constexpr size_t kMaxTextUnits=65536;
constexpr uint64_t kMaxSamples=44100ull*3600;

struct Runtime {
    HMODULE module=nullptr;
    decltype(&SherpaOnnxGetVersionStr) version=nullptr;
    decltype(&SherpaOnnxCreateOfflineTts) create=nullptr;
    decltype(&SherpaOnnxDestroyOfflineTts) destroy=nullptr;
    decltype(&SherpaOnnxOfflineTtsGenerateWithConfig) generate=nullptr;
    decltype(&SherpaOnnxDestroyOfflineTtsGeneratedAudio) free_audio=nullptr;
    template<class T> T symbol(const char* name) {
        auto address=GetProcAddress(module,name);
        if(!address)throw std::runtime_error("Local TTS runtime has an incompatible C API");
        return reinterpret_cast<T>(address);
    }
    explicit Runtime(const fs::path& directory) {
        module=LoadLibraryExW((directory/L"sherpa-onnx-c-api.dll").c_str(),nullptr,
                             LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_SYSTEM32);
        if(!module)throw std::runtime_error("Cannot load the packaged local TTS runtime");
        try {
            version=symbol<decltype(version)>("SherpaOnnxGetVersionStr");
            create=symbol<decltype(create)>("SherpaOnnxCreateOfflineTts");
            destroy=symbol<decltype(destroy)>("SherpaOnnxDestroyOfflineTts");
            generate=symbol<decltype(generate)>("SherpaOnnxOfflineTtsGenerateWithConfig");
            free_audio=symbol<decltype(free_audio)>("SherpaOnnxDestroyOfflineTtsGeneratedAudio");
            if(!version()||std::string(version())!="2.0.0")throw std::runtime_error("Unexpected local TTS runtime version");
        } catch(...) {FreeLibrary(module);module=nullptr;throw;}
    }
    ~Runtime(){if(module)FreeLibrary(module);}
    Runtime(const Runtime&)=delete;
    Runtime& operator=(const Runtime&)=delete;
};

fs::path executable_directory() {
    std::vector<wchar_t> buffer(32768);
    DWORD count=GetModuleFileNameW(nullptr,buffer.data(),DWORD(buffer.size()));
    if(!count||count>=buffer.size())throw std::runtime_error("Cannot locate local TTS helper");
    return fs::path(std::wstring(buffer.data(),count)).parent_path();
}

bool model_present(const fs::path& directory) {
    try {
        return fs::is_regular_file(directory/L"model.int8.onnx")&&fs::file_size(directory/L"model.int8.onnx")==53517430&&
               fs::is_regular_file(directory/L"lexicon.txt")&&fs::file_size(directory/L"lexicon.txt")==6837671&&
               fs::is_regular_file(directory/L"tokens.txt")&&fs::file_size(directory/L"tokens.txt")==655&&
               fs::is_regular_file(directory/L"phone.fst")&&fs::is_regular_file(directory/L"date.fst")&&fs::is_regular_file(directory/L"number.fst");
    } catch(const fs::filesystem_error&){return false;}
}

std::wstring read_text(const fs::path& file) {
    std::ifstream input(file,std::ios::binary|std::ios::ate);
    if(!input)throw std::runtime_error("Cannot read the UTF-8 text file");
    auto length=input.tellg();
    if(length<=0||uint64_t(length)>kMaxTextBytes)throw std::runtime_error("Text file is empty or exceeds the local TTS limit");
    std::string bytes(size_t(length),'\0');input.seekg(0);
    if(!input.read(bytes.data(),std::streamsize(bytes.size())))throw std::runtime_error("Cannot read complete text file");
    if(bytes.size()>=3&&bytes.compare(0,3,"\xef\xbb\xbf")==0)bytes.erase(0,3);
    if(bytes.find('\0')!=std::string::npos)throw std::runtime_error("Text contains a NUL character");
    int count=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,bytes.data(),int(bytes.size()),nullptr,0);
    if(count<=0||size_t(count)>kMaxTextUnits)throw std::runtime_error("Text must be valid UTF-8 within the local TTS limit");
    std::wstring text(size_t(count),L'\0');
    if(!MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,bytes.data(),int(bytes.size()),text.data(),count))throw std::runtime_error("UTF-8 conversion failed");
    if(text.find_first_not_of(L" \t\r\n")==std::wstring::npos)throw std::runtime_error("Text contains no pronounceable content");
    return text;
}

std::string utf8(const std::wstring& text) {
    int count=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,text.data(),int(text.size()),nullptr,0,nullptr,nullptr);
    if(count<=0)throw std::runtime_error("Text conversion failed");
    std::string result(size_t(count),'\0');
    if(!WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,text.data(),int(text.size()),result.data(),count,nullptr,nullptr))throw std::runtime_error("Text conversion failed");
    return result;
}

std::vector<std::wstring> chunks(const std::wstring& text) {
    std::vector<std::wstring> result;
    for(size_t start=0;start<text.size();) {
        size_t stop=std::min(start+220,text.size());
        if(stop<text.size()) {
            for(size_t index=stop;index>start+50;--index) {
                wchar_t ch=text[index-1];
                if(ch==L'。'||ch==L'！'||ch==L'？'||ch==L'；'||ch==L'\n'||ch==L' '){stop=index;break;}
            }
            if(stop>start&&text[stop-1]>=0xd800&&text[stop-1]<=0xdbff)--stop;
        }
        if(stop<=start)throw std::runtime_error("Cannot split text safely");
        result.push_back(text.substr(start,stop-start));start=stop;
    }
    return result;
}

struct WaveOutput {
    HANDLE handle=INVALID_HANDLE_VALUE;
    fs::path path;
    uint64_t samples=0;
    int sample_rate=0;
    bool complete=false;
    explicit WaveOutput(const fs::path& value):path(value) {
        handle=CreateFileW(path.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
        if(handle==INVALID_HANDLE_VALUE)throw std::runtime_error("WAV output must be a new writable file");
        try {unsigned char header[44]={};write(header,sizeof(header));}
        catch(...) {CloseHandle(handle);handle=INVALID_HANDLE_VALUE;DeleteFileW(path.c_str());throw;}
    }
    void write(const void* data,size_t length) {
        auto pointer=static_cast<const unsigned char*>(data);
        while(length) {
            DWORD portion=DWORD(std::min<size_t>(length,1024*1024)),written=0;
            if(!WriteFile(handle,pointer,portion,&written,nullptr)||written!=portion)throw std::runtime_error("Cannot write complete WAV output");
            pointer+=written;length-=written;
        }
    }
    void append(const SherpaOnnxGeneratedAudio* audio) {
        if(!audio||!audio->samples||audio->n<=0||audio->sample_rate!=44100)throw std::runtime_error("Local TTS produced no valid 44.1 kHz audio");
        if(sample_rate&&sample_rate!=audio->sample_rate)throw std::runtime_error("Local TTS changed sample rate");
        sample_rate=audio->sample_rate;
        if(samples+uint64_t(audio->n)>kMaxSamples)throw std::runtime_error("Local TTS audio exceeds its bounded duration");
        std::vector<int16_t> pcm(size_t(audio->n));
        for(size_t index=0;index<pcm.size();++index) {
            float value=audio->samples[index];
            if(!std::isfinite(value))throw std::runtime_error("Local TTS generated an invalid sample");
            pcm[index]=int16_t(std::lround(std::clamp(value,-1.f,1.f)*32767.f));
        }
        write(pcm.data(),pcm.size()*sizeof(int16_t));samples+=pcm.size();
    }
    static void u16(unsigned char* p,uint16_t value){p[0]=value&255;p[1]=(value>>8)&255;}
    static void u32(unsigned char* p,uint32_t value){for(int index=0;index<4;index++)p[index]=(value>>(index*8))&255;}
    void finish() {
        if(!samples||!sample_rate)throw std::runtime_error("Local TTS produced no audio");
        unsigned char header[44]={};
        memcpy(header,"RIFF",4);u32(header+4,uint32_t(samples*2+36));memcpy(header+8,"WAVEfmt ",8);
        u32(header+16,16);u16(header+20,1);u16(header+22,1);u32(header+24,uint32_t(sample_rate));
        u32(header+28,uint32_t(sample_rate*2));u16(header+32,2);u16(header+34,16);memcpy(header+36,"data",4);u32(header+40,uint32_t(samples*2));
        LARGE_INTEGER zero={};
        if(!SetFilePointerEx(handle,zero,nullptr,FILE_BEGIN))throw std::runtime_error("Cannot finalize WAV output");
        write(header,sizeof(header));if(!FlushFileBuffers(handle))throw std::runtime_error("Cannot flush WAV output");complete=true;
    }
    ~WaveOutput(){if(handle!=INVALID_HANDLE_VALUE)CloseHandle(handle);if(!complete)DeleteFileW(path.c_str());}
};

int wmain(int argc,wchar_t** argv) {
    SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
    try {
        auto directory=executable_directory();fs::path text_file,wav_file,model_directory=directory/L"model";
        int rate=0;bool probe=false,list=false;
        for(int index=1;index<argc;index++) {
            std::wstring arg=argv[index];
            if(arg==L"--probe"){probe=true;continue;}
            if(arg==L"--list"){list=true;continue;}
            if(index+1>=argc)throw std::runtime_error("Missing local TTS argument value");
            if(arg==L"--text")text_file=fs::absolute(argv[++index]);
            else if(arg==L"--wav")wav_file=fs::absolute(argv[++index]);
            else if(arg==L"--model-dir")model_directory=fs::absolute(argv[++index]);
            else if(arg==L"--rate") {
                const wchar_t* raw=argv[++index];wchar_t* end=nullptr;long value=wcstol(raw,&end,10);
                if(!*raw||!end||*end||value< -3||value>3)throw std::runtime_error("Rate must be an integer from -3 to 3");rate=int(value);
            } else throw std::runtime_error("Unknown local TTS option");
        }
        bool present=model_present(model_directory);
        if(probe) {
            try {
                Runtime runtime(directory);
                std::cout<<"{\"available\":"<<(present?"true":"false")<<",\"backend\":\"local-melo\",\"runtime_version\":\"2.0.0\",\"model\":\"melo-zh-en-female-int8\",\"language\":\"zh-CN\",\"supports_traditional_text\":true,\"provider\":\"cpu\",\"threads\":2,\"model_present\":"<<(present?"true":"false")<<",\"hashes_checked\":false,\"model_loaded\":false,\"playback\":false,\"max_text_bytes\":262144,\"max_text_utf16_units\":65536,\"max_synthesis_seconds\":600,\"max_audio_seconds\":3600}";
            } catch(const std::exception&) {
                std::cout<<"{\"available\":false,\"backend\":\"local-melo\",\"runtime_version\":null,\"model\":\"melo-zh-en-female-int8\",\"model_loaded\":false,\"playback\":false,\"error\":\"runtime_unavailable\"}";
            }
            return 0;
        }
        if(list) {
            if(!present){std::cout<<"[]";return 0;}
            try {Runtime runtime(directory);
                std::cout<<"[{\"id\":\"melo-zh-en-female-int8\",\"name\":\"MeloTTS Mandarin + English (local female)\",\"language\":\"zh-CN\",\"supports_traditional_text\":true,\"backend\":\"local-melo\"}]";
            } catch(const std::exception&){std::cout<<"[]";}
            return 0;
        }
        if(text_file.empty()||wav_file.empty()||!present)throw std::runtime_error("Pass --text and --wav with a prepared local model");
        Runtime runtime(directory);
        auto input=read_text(text_file);auto pieces=chunks(input);
        // Upstream accepts narrow paths. ASCII relative names plus the native
        // wide current directory preserve Unicode/space package parent paths.
        if(!SetCurrentDirectoryW(model_directory.c_str()))throw std::runtime_error("Cannot enter local model directory");
        SherpaOnnxOfflineTtsConfig config={};
        config.model.vits.model="model.int8.onnx";config.model.vits.lexicon="lexicon.txt";config.model.vits.tokens="tokens.txt";
        config.model.vits.noise_scale=.667f;config.model.vits.noise_scale_w=.8f;config.model.vits.length_scale=1.f;
        config.model.num_threads=2;config.model.provider="cpu";config.model.debug=0;
        config.rule_fsts="phone.fst,date.fst,number.fst";config.max_num_sentences=1;config.silence_scale=.2f;
        const auto* tts=runtime.create(&config);
        if(!tts)throw std::runtime_error("Cannot create local CPU speech model");
        auto guard=std::unique_ptr<const SherpaOnnxOfflineTts,std::function<void(const SherpaOnnxOfflineTts*)>>(tts,[&runtime](const auto* value){runtime.destroy(value);});
        WaveOutput output(wav_file);auto deadline=Clock::now()+std::chrono::seconds(600);
        SherpaOnnxGenerationConfig generation={};generation.sid=0;generation.speed=std::pow(1.13f,float(rate));generation.silence_scale=.2f;
        for(const auto& piece:pieces) {
            if(Clock::now()>deadline)throw std::runtime_error("Local TTS synthesis timed out");
            if(piece.find_first_not_of(L" \r\n\t")==std::wstring::npos)continue;
            auto text=utf8(piece);
            auto callback=[](const float*,int32_t,float,void* arg)->int32_t{return Clock::now()<=*static_cast<Clock::time_point*>(arg)?1:0;};
            const auto* audio=runtime.generate(tts,text.c_str(),&generation,callback,&deadline);
            auto audio_guard=std::unique_ptr<const SherpaOnnxGeneratedAudio,std::function<void(const SherpaOnnxGeneratedAudio*)>>(audio,[&runtime](const auto* value){if(value)runtime.free_audio(value);});
            if(Clock::now()>deadline)throw std::runtime_error("Local TTS synthesis timed out");
            output.append(audio);
        }
        output.finish();
        std::cout<<"{\"complete\":true,\"backend\":\"local\",\"sample_rate\":"<<output.sample_rate<<",\"seconds\":"<<double(output.samples)/output.sample_rate<<",\"text_units\":"<<input.size()<<",\"chunks\":"<<pieces.size()<<",\"playback\":false}";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what();return 1;}
}
