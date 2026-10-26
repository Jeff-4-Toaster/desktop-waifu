#define UNICODE
#define _UNICODE
#define NOMINMAX
#include <windows.h>
#include <sapi.h>
#include <wrl/client.h>
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
using Microsoft::WRL::ComPtr;
std::string utf8(const std::wstring& value) {int size=WideCharToMultiByte(CP_UTF8,0,value.data(),int(value.size()),nullptr,0,nullptr,nullptr);std::string result(size,0);WideCharToMultiByte(CP_UTF8,0,value.data(),int(value.size()),result.data(),size,nullptr,nullptr);return result;}
std::wstring wide(const std::string& value) {int size=MultiByteToWideChar(CP_UTF8,0,value.data(),int(value.size()),nullptr,0);std::wstring result(size,0);MultiByteToWideChar(CP_UTF8,0,value.data(),int(value.size()),result.data(),size);return result;}
std::string quote(const std::wstring& value) {std::string out="\"";for(unsigned char c:utf8(value)) {if(c=='"'||c=='\\')out+='\\';if(c>=32)out+=c;}return out+'"';}
HRESULT tokens(const wchar_t* filter,IEnumSpObjectTokens** result) {ComPtr<ISpObjectTokenCategory> category;HRESULT hr=CoCreateInstance(CLSID_SpObjectTokenCategory,nullptr,CLSCTX_ALL,IID_PPV_ARGS(&category));if(SUCCEEDED(hr))hr=category->SetId(SPCAT_VOICES,FALSE);if(SUCCEEDED(hr))hr=category->EnumTokens(filter,nullptr,result);return hr;}
int wmain(int argc,wchar_t** argv) {
    if(FAILED(CoInitializeEx(nullptr,COINIT_APARTMENTTHREADED))) return 2;
    int result=0;
    {ComPtr<ISpVoice> voice;HRESULT hr=CoCreateInstance(CLSID_SpVoice,nullptr,CLSCTX_ALL,IID_PPV_ARGS(&voice));
    bool list=false;std::filesystem::path textPath,wavPath;int rate=0;
    for(int i=1;i<argc;i++) {std::wstring arg=argv[i];if(arg==L"--list") list=true;else if(arg==L"--text"&&i+1<argc) textPath=argv[++i];else if(arg==L"--wav"&&i+1<argc) wavPath=argv[++i];else if(arg==L"--rate"&&i+1<argc) rate=std::clamp(_wtoi(argv[++i]),-3,3);}
    if(SUCCEEDED(hr)&&list) {
        ComPtr<IEnumSpObjectTokens> enumerator;hr=tokens(nullptr,&enumerator);std::cout<<"[";bool first=true;
        if(SUCCEEDED(hr)) {ComPtr<ISpObjectToken> token;while(enumerator->Next(1,&token,nullptr)==S_OK) {wchar_t* name{};wchar_t* id{};wchar_t* language{};token->GetStringValue(nullptr,&name);token->GetId(&id);ComPtr<ISpDataKey> attributes;if(SUCCEEDED(token->OpenKey(L"Attributes",&attributes)))attributes->GetStringValue(L"Language",&language);if(!first)std::cout<<",";first=false;std::cout<<"{\"name\":"<<quote(name?name:L"")<<",\"id\":"<<quote(id?id:L"")<<",\"language\":"<<quote(language?language:L"")<<"}";CoTaskMemFree(name);CoTaskMemFree(id);CoTaskMemFree(language);token.Reset();}}
        std::cout<<"]";
    } else if(SUCCEEDED(hr)&&!textPath.empty()&&!wavPath.empty()) {
        std::ifstream file(textPath,std::ios::binary);std::string text((std::istreambuf_iterator<char>(file)),{});
        if(!file.good()&&!file.eof()) hr=E_FAIL;
        ComPtr<IEnumSpObjectTokens> choices;ComPtr<ISpObjectToken> chinese;if(SUCCEEDED(tokens(L"Language=404",&choices))&&choices->Next(1,&chinese,nullptr)==S_OK)hr=voice->SetVoice(chinese.Get());else hr=E_FAIL;
        voice->SetRate(rate);WAVEFORMATEX format{WAVE_FORMAT_PCM,1,16000,32000,2,16,0};ComPtr<ISpStream> stream;
        if(SUCCEEDED(hr)) hr=CoCreateInstance(CLSID_SpStream,nullptr,CLSCTX_ALL,IID_PPV_ARGS(&stream));
        if(SUCCEEDED(hr)) hr=stream->BindToFile(wavPath.c_str(),SPFM_CREATE_ALWAYS,&SPDFID_WaveFormatEx,&format,0);
        if(SUCCEEDED(hr)) hr=voice->SetOutput(stream.Get(),TRUE);
        if(SUCCEEDED(hr)) hr=voice->Speak(wide(text).c_str(),SPF_IS_NOT_XML,nullptr);
        if(stream) stream->Close();
    } else hr=E_INVALIDARG;
    if(FAILED(hr)) {std::cerr<<"Windows speech failed: "<<std::hex<<hr;result=1;}}
    CoUninitialize();return result;
}
