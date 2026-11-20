using System;
using System.IO;
using System.Runtime.InteropServices;
class PythonHost {
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern bool SetDllDirectory(string path);
  [DllImport("python314.dll", CallingConvention=CallingConvention.Cdecl)] static extern void Py_Initialize();
  [DllImport("python314.dll", CallingConvention=CallingConvention.Cdecl)] static extern int PyRun_SimpleString([MarshalAs(UnmanagedType.LPUTF8Str)] string code);
  [DllImport("python314.dll", CallingConvention=CallingConvention.Cdecl)] static extern int Py_FinalizeEx();
  static int Main(string[] args) {
    string runtime=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"runtime");
    SetDllDirectory(runtime);
    Environment.SetEnvironmentVariable("PYTHONHOME",runtime);
    Environment.SetEnvironmentVariable("PYTHONPATH",runtime+";"+Path.Combine(runtime,"base_library.zip"));
    Py_Initialize();
    int status=PyRun_SimpleString(File.ReadAllText(args[0]));
    Py_FinalizeEx(); return status==0?0:1;
  }
}
