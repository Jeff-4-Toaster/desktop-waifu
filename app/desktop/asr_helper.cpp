#define UNICODE
#define _UNICODE
#define NOMINMAX
#include <windows.h>
#include <filesystem>
#include <fstream>
#include <vector>
#include <string>
#include <stdexcept>
#include <cstdint>
#include <cstring>
#include <iostream>
#include "whisper.h"
namespace fs=std::filesystem;
std::vector<unsigned char> bytes(const fs::path& path,size_t limit) {
    std::ifstream stream(path,std::ios::binary|std::ios::ate);if(!stream)throw std::runtime_error("Cannot read input");
    auto length=stream.tellg();if(length<=0||uint64_t(length)>limit)throw std::runtime_error("Invalid input size");
    std::vector<unsigned char> data(size_t(length),0);stream.seekg(0);if(!stream.read(reinterpret_cast<char*>(data.data()),std::streamsize(data.size())))throw std::runtime_error("Cannot read complete input");return data;
}
uint32_t u32(const unsigned char* p){return uint32_t(p[0])|(uint32_t(p[1])<<8)|(uint32_t(p[2])<<16)|(uint32_t(p[3])<<24);}
uint16_t u16(const unsigned char* p){return uint16_t(p[0])|(uint16_t(p[1])<<8);}
int wmain(int argc,wchar_t** argv) {
    try {
        fs::path model,wav,output;
        for(int i=1;i+1<argc;i++) {std::wstring arg=argv[i];if(arg==L"--model")model=argv[++i];else if(arg==L"--wav")wav=argv[++i];else if(arg==L"--out")output=argv[++i];}
        if(model.empty()||wav.empty()||output.empty())throw std::runtime_error("Pass --model, --wav and --out");
        auto audio=bytes(wav,2*1024*1024);if(audio.size()<44||memcmp(audio.data(),"RIFF",4)||memcmp(audio.data()+8,"WAVE",4))throw std::runtime_error("Invalid WAV");
        bool validFormat=false;std::vector<float> samples;
        for(size_t pos=12;pos+8<=audio.size();) {
            size_t count=u32(audio.data()+pos+4);if(count>audio.size()-pos-8)throw std::runtime_error("Truncated WAV");
            auto chunk=audio.data()+pos+8;
            if(!memcmp(audio.data()+pos,"fmt ",4)&&count>=16)validFormat=u16(chunk)==1&&u16(chunk+2)==1&&u32(chunk+4)==16000&&u16(chunk+14)==16;
            if(!memcmp(audio.data()+pos,"data",4)) {if(count%2||count>16000*31*2)throw std::runtime_error("Invalid audio duration");samples.reserve(count/2);for(size_t i=0;i<count;i+=2)samples.push_back(int16_t(u16(chunk+i))/32768.f);}
            pos+=8+count+(count&1);
        }
        if(!validFormat||samples.size()<2400)throw std::runtime_error("Expected mono 16 kHz PCM16 WAV");
        // Wide Windows paths stay in project-owned I/O. The unmodified SDK reads a buffer.
        auto weights=bytes(model,512*1024*1024);
        whisper_log_set([](ggml_log_level,const char*,void*){},nullptr);
        auto contextParams=whisper_context_default_params();contextParams.use_gpu=false;contextParams.flash_attn=false;
        auto context=whisper_init_from_buffer_with_params(weights.data(),weights.size(),contextParams);if(!context)throw std::runtime_error("Cannot load local speech model");
        auto params=whisper_full_default_params(WHISPER_SAMPLING_GREEDY);params.n_threads=6;params.language="zh";params.translate=false;params.no_context=true;params.no_timestamps=true;
        params.print_progress=false;params.print_realtime=false;params.print_timestamps=false;params.print_special=false;params.greedy.best_of=1;params.initial_prompt="以下是繁體中文對話。";
        int status=whisper_full(context,params,samples.data(),int(samples.size()));std::string text;
        if(status==0)for(int i=0;i<whisper_full_n_segments(context);i++){const char* segment=whisper_full_get_segment_text(context,i);if(segment)text+=segment;}
        whisper_free(context);if(status||text.empty())throw std::runtime_error("Local transcription failed");
        std::ofstream file(output,std::ios::binary);file<<text;if(!file)throw std::runtime_error("Cannot save transcript");return 0;
    }catch(const std::exception& error){std::cerr<<error.what();return 1;}
}
