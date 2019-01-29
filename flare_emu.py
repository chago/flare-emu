############################################
# Copyright (C) 2018 FireEye, Inc.
#
# Licensed under the Apache License, Version 2.0, <LICENSE-APACHE or
# http://apache.org/licenses/LICENSE-2.0> or the MIT license <LICENSE-BSD-3-CLAUSE or
# https://opensource.org/licenses/BSD-3-Clause>, at your option. This file may not be
# copied, modified, or distributed except according to those terms.
#
# Author: James T. Bennett
#
# flare-emu combines Unicorn and IDA to provide emulation support for
# reverse engineers
# Currently supports 32-bit and 64-bit x86, ARM, and ARM64
# Dependencies:
# https://github.com/unicorn-engine/unicorn
############################################

from __future__ import print_function
import idc
import idaapi
import idautils
import unicorn
import unicorn.x86_const
import unicorn.arm_const
import unicorn.arm64_const
from copy import deepcopy
import logging
import struct
import re

IDADIR = idc.idadir()
PAGESIZE = 0x1000
PAGEALIGNCHECK = 0xfff
X86NOP = "\x90"
ARMTHUMBNOP = "\x00\xbf"
ARMNOP = "\x00\xf0\x20\xe3"
ARM64NOP = "\x1f\x20\x03\xd5"
MAX_ALLOC_SIZE = 10 * 1024 * 1024

try:
    long        # Python 2
except NameError:
    long = int  # Python 3

class EmuHelper():
    def __init__(self, verbose = 0):
        self.verbose = verbose
        self.stack = 0
        self.stackSize = 0x2000
        self.size_DWORD = 4
        self.size_pointer = 0
        self.callMnems = ["call", "BL", "BLX", "BLR",
                          "BLXEQ", "BLEQ", "BLREQ"]
        self.paths = {}
        self.filetype = "UNKNOWN"
        self.uc = None
        self.h_userhook = None
        self.h_memaccesshook = None
        self.h_codehook = None
        self.h_memhook = None
        self.h_inthook = None
        self.enteredBlock = False
        self.initEmuHelper()
        self.reloadBinary()

    # startAddr: address to start emulation
    # endAddr: address to end emulation, this instruction is not executed. 
    #     if not provided, emulation stops when starting function is exited 
    #     (function must end with a return instruction)
    # registers: a dict whose keys are register names and values are
    #     register values, all unspecified registers will be initialized to 0
    # stack: a list of values to be setup on the stack before emulation.
    #     if X86 you must account for SP+0 (return address).
    #     for the stack and registers parameters, specifying a string will 
    #     allocate memory, write the string to it, and write a pointer to that 
    #     memory in the specified register/arg
    # instructionHook: instruction hook func that runs AFTER emulateRange's hook
    # hookData: user-defined data to be made available in instruction hook
    #     function, care must be taken to not use key names already used by
    #     flare_emu in userData dictionary
    # skipCalls: emulator will skip over call instructions and adjust the
    #     stack accordingly, defaults to True
    # emulateRange will always skip over calls to empty memory
    # callHook: callback function that will be called whenever the emulator
    #     encounters a "call" instruction. keep in mind your skipCalls value
    #     and that emulateRange will always skip over calls to empty memory
    # memAccessHook: hook function that runs when the emulator encounters a
    #     memory read or write
    # hookApis: set to False if you don't want flare-emu to emulate common 
    #     runtime memory and string functions, defaults to True
    # returns the emulation object in its state after the emulation completes
    # count: Value passed to unicorn's uc_emu_start to indicate max number of
    #     instructions to emulate, Defaults to 0 (all code available).
    def emulateRange(self, startAddr, endAddr=None, registers=None, stack=None, instructionHook=None, callHook=None,
                     memAccessHook=None, hookData=None, skipCalls=True, hookApis=True, count=0):
        if registers is None:
            registers = {}
        if stack is None:
            stack = []
        userData = {"EmuHelper": self, "funcStart": idc.get_func_attr(startAddr, idc.FUNCATTR_START),
                    "funcEnd": idc.get_func_attr(startAddr, idc.FUNCATTR_END), "skipCalls": skipCalls,
                    "endAddr": endAddr, "func_t": idaapi.get_func(startAddr), "callHook": callHook, "hookApis": hookApis, "count": count}
        if hookData:
            userData.update(hookData)
        mu = self.uc
        self._prepEmuContext(registers, stack)
        self.resetEmuHooks()
        self.h_codehook = mu.hook_add(
            unicorn.UC_HOOK_CODE, self._emulateRangeCodeHook, userData)
        if instructionHook:
            self.h_userhook = mu.hook_add(unicorn.UC_HOOK_CODE, instructionHook, userData)
        if memAccessHook:
            self.h_memaccesshook = self.uc.hook_add(unicorn.UC_HOOK_MEM_READ | unicorn.UC_HOOK_MEM_WRITE, memAccessHook,
                                                    userData)
        self.h_memhook = mu.hook_add(unicorn.UC_HOOK_MEM_READ_UNMAPPED | unicorn.UC_HOOK_MEM_WRITE_UNMAPPED |
                                     unicorn.UC_HOOK_MEM_FETCH_UNMAPPED, self._hookMemInvalid, userData)
        self.h_inthook = mu.hook_add(
            unicorn.UC_HOOK_INTR, self._hookInterrupt, userData)
        if self.arch == unicorn.UC_ARCH_ARM:
            userData["changeThumbMode"] = True
        mu.emu_start(startAddr, userData["funcEnd"], count=count)
        return mu
        
    # call emulateRange using selected instructions in IDA Pro as start/end addresses
    def emulateSelection(self, registers=None, stack=None, instructionHook=None, callHook=None,
                     memAccessHook=None, hookData=None, skipCalls=True, hookApis=True, count=0):
        selection = idaapi.read_selection()
        if selection[0]:
            self.emulateRange(selection[1], selection[2], registers, stack, instructionHook, 
                              callHook, memAccessHook, hookData, skipCalls, hookApis, count=count)

    # target: finds first path through function to target using depth first
    #     search for each address in list, if a single address is specified,
    #     does so for each xref to target address
    #     emulates each target's function, forcing path to target, then
    #     executes callback function providing emu object and arguments
    # instructionHook: user-defined instruction hook to run AFTER guidedHook that
    #     forces execution
    # hookData: user-defined data to be made available in instruction hook
    #     function, care must be taken to not use key names already used by
    #     flare_emu in userData dictionary
    # preEmuCallback: a callback that is called BEFORE each emulation run
    # callHook: a callback that is called whenever the emulator encounters a
    #     "call" instruction. hook or no, after a call instruction, the
    #     program counter is advanced to the next instruction and the stack is
    #     automatically cleaned up
    # resetEmuMem: if set to True, unmaps all allocated emulator memory and
    #     reloads the binary from the IDB into emulator memory before each
    #     emulation run. can significantly increase script run time, defaults
    #     to False
    # hookApis: set to False if you don't want flare-emu to emulate common 
    # runtime memory and string functions, defaults to True
    # memAccessHook: hook function that runs when the emulator encounters a
    #     memory read or write
    def iterate(self, target, targetCallback, preEmuCallback=None, callHook=None, instructionHook=None,
                hookData=None, resetEmuMem=False, hookApis=True, memAccessHook=None):
        if target is None:
            return

        targetInfo = {}
        if type(target) in [int, long]:
            logging.debug("iterate target function: %s" %
                          self.hexString(target))
            xrefs = list(idautils.XrefsTo(target))
            for i, x in enumerate(xrefs):
                # get unique functions from xrefs that we need to emulate
                funcStart = idc.get_func_attr(x.frm, idc.FUNCATTR_START)
                if funcStart == idc.BADADDR:
                    continue
                if idc.print_insn_mnem(x.frm) not in ["call", "jmp", "BL", "BLX", "B", "BLR"]:
                    continue

                logging.debug("getting a path to %s, %d of %d" %
                              (self.hexString(x.frm), i + 1, len(xrefs)))
                flow, paths = self.getPath(x.frm)
                if flow is not None:
                    targetInfo[x.frm] = (flow, paths)
        elif isinstance(target, list):
            for i, t in enumerate(target):
                logging.debug("getting a path to %s, %d of %d" %
                              (self.hexString(t), i + 1, len(target)))
                flow, paths = self.getPath(t)
                if flow is not None:
                    targetInfo[t] = (flow, paths)
        if len(targetInfo) <= 0:
            logging.debug("no targets to iterate")
            return

        userData = {}
        userData["targetInfo"] = targetInfo
        userData["targetCallback"] = targetCallback
        userData["callHook"] = callHook
        userData["EmuHelper"] = self
        userData["hookApis"] = hookApis
        if hookData:
            userData.update(hookData)
        self.internalRun = False
        self.resetEmuHooks()
        self.h_codehook = self.uc.hook_add(
            unicorn.UC_HOOK_CODE, self._guidedHook, userData)
        if instructionHook:
            self.h_userhook = self.uc.hook_add(unicorn.UC_HOOK_CODE, instructionHook, userData)
        if memAccessHook:
            self.h_memaccesshook = self.uc.hook_add(unicorn.UC_HOOK_MEM_READ | unicorn.UC_HOOK_MEM_WRITE, memAccessHook,
                                                    userData)
        self.h_memhook = self.uc.hook_add(unicorn.UC_HOOK_MEM_READ_UNMAPPED | unicorn.UC_HOOK_MEM_WRITE_UNMAPPED |
                                          unicorn.UC_HOOK_MEM_FETCH_UNMAPPED, self._hookMemInvalid, userData)
        self.h_inthook = self.uc.hook_add(
            unicorn.UC_HOOK_INTR, self._hookInterrupt, userData)
        self.blockIdx = 0
        cnt = 1

        # read targets from dict to go from higher to lower addresses
        # this is done to optimize loop by allowing hook to check for and remove other targets visited en route to
        # current target
        while len(userData["targetInfo"]) > 0:
            userData["targetVA"] = targetVA = sorted(
                userData["targetInfo"].keys(), reverse=True)[0]
            flow, paths = userData["targetInfo"][targetVA]
            funcStart = flow[0][0]
            userData["func_t"] = idaapi.get_func(funcStart)
            self.pathIdx = 0
            numTargets = len(userData["targetInfo"])
            logging.debug("run #%d, %d targets remaining: %s (%d paths)" % (
                cnt, numTargets, self.hexString(targetVA), len(paths)))
            cnt2 = 1
            numPaths = len(paths)
            for path in paths:
                logging.debug("emulating path #%d of %d from %s to %s via basic blocks: %s" % (
                    cnt2, numPaths, self.hexString(funcStart), self.hexString(targetVA), repr(path)))
                for reg in self.regs:
                    self.uc.reg_write(self.regs[reg], 0)
                if resetEmuMem:
                    self.reloadBinary()
                self.uc.reg_write(self.regs["sp"], self.stack)
                self.enteredBlock = False
                userData["visitedTargets"] = []
                if preEmuCallback:
                    preEmuCallback(self, userData, funcStart)
                if self.arch == unicorn.UC_ARCH_ARM:
                    userData["changeThumbMode"] = True

                self.uc.emu_start(funcStart, idc.get_func_attr(
                    funcStart, idc.FUNCATTR_END))
                self.pathIdx += 1
                self.blockIdx = 0
                cnt2 += 1
                # remove visited targets during this run from our dict
                for addr in userData["visitedTargets"]:
                    del(userData["targetInfo"][addr])

            cnt += 1

    # simply emulates to the end of whatever bytes are provided
    # these bytes are not loaded into IDB, only emulator memory; IDA APIs are not available for use in hooks here
    def emulateBytes(self, bytes, registers=None, stack=None, baseAddr=0x400000, instructionHook=None,
                     memAccessHook=None, hookData=None):
        if registers is None:
            registers = {}
        if stack is None:
            stack = []
        userData = {}
        if hookData:
            userData.update(hookData)
        baseAddr = self.loadBytes(bytes, baseAddr)
        endAddr = baseAddr + len(bytes)
        userData["endAddr"] = endAddr
        mu = self.uc
        self._prepEmuContext(registers, stack)
        self.resetEmuHooks()
        self.h_codehook = mu.hook_add(
            unicorn.UC_HOOK_CODE, self._emulateBytesCodeHook, userData)
        if instructionHook:
            self.h_userhook = mu.hook_add(unicorn.UC_HOOK_CODE, instructionHook, userData)
        if memAccessHook:
            self.h_memaccesshook = self.uc.hook_add(unicorn.UC_HOOK_MEM_READ | unicorn.UC_HOOK_MEM_WRITE, memAccessHook,
                                                    userData)
        self.h_memhook = mu.hook_add(unicorn.UC_HOOK_MEM_READ_UNMAPPED | unicorn.UC_HOOK_MEM_WRITE_UNMAPPED |
                                     unicorn.UC_HOOK_MEM_FETCH_UNMAPPED, self._hookMemInvalid, userData)
        self.h_inthook = mu.hook_add(
            unicorn.UC_HOOK_INTR, self._hookInterrupt, userData)
        mu.emu_start(baseAddr, endAddr)
        return mu

    def hexString(self, va):
        if va > 0xffffffff:
            return "%016X" % va
        else:
            return "%08X" % va

    def pageAlignUp(self, v):
        if v & PAGEALIGNCHECK != 0:
            v += PAGESIZE - (v % PAGESIZE)
        return v

    # returns string of bytes from the IDB up to a null terminator, starting at addr, do not necessarily need to be printable
    # characters
    def getIDBString(self, addr):
        buf = ""
        while idc.get_bytes(addr, 1, False) != "\x00" and idc.get_bytes(addr, 1, False) is not None:
            buf += idc.get_bytes(addr, 1, False)
            addr += 1

        return buf

    # determines if the instruction at addr is for returning from a function call
    def isRetInstruction(self, addr):
        if idc.print_insn_mnem(addr)[:3].lower() == "ret":
            return True

        if idc.print_insn_mnem(addr) in ["BX", "B"] and idc.print_operand(addr, 0) == "LR":
            return True

        return False

    # call from an emulation hook to skip the current instruction, moving pc to next instruction
    # useIDA option was added to handle cases where IDA folds multiple instructions
    # do not call multiple times in a row, depends on userData being updated by hook
    def skipInstruction(self, userData, useIDA=False):
        if self.arch == unicorn.UC_ARCH_ARM:
            userData["changeThumbMode"] = True
        if useIDA:
            self.uc.reg_write(self.regs["pc"], idc.next_head(
                userData["currAddr"], idc.get_inf_attr(idc.INF_MAX_EA)))
        else:
            self.uc.reg_write(
                self.regs["pc"], userData["currAddr"] + userData["currAddrSize"])
        # get IDA's SP delta value for next instruction to adjust stack accordingly since we are skipping
        # this instruction
        self.uc.reg_write(self.regs["sp"], self.getRegVal(
            "sp") + idaapi.get_sp_delta(userData["func_t"], idc.next_head(
            userData["currAddr"], idc.get_inf_attr(idc.INF_MAX_EA))))
            
    # call from an emulation hook to change program counter
    def changeProgramCounter(self, userData, newPC):
        if self.arch == unicorn.UC_ARCH_ARM:
            userData["changeThumbMode"] = True
        self.uc.reg_write(self.regs["pc"], newPC)

    # retrieves the value of a register, handling subregister addressing
    def getRegVal(self, regName):
        regVal = self.uc.reg_read(self.regs[regName])
        # handle various subregister addressing
        if self.arch == unicorn.UC_ARCH_X86:
            if regName[:-1] in ["l", "b"]:
                regVal = regVal & 0xFF
            elif regName[:-1] == "h":
                regVal = (regVal & 0xFF00) >> 8
            elif len(regName) == 2 and regName[:-1] == "x":
                regVal = regVal & 0xFFFF
        elif self.arch == unicorn.UC_ARCH_ARM64:
            if regName[0] == "W":
                regVal = regVal & 0xFFFFFFFF
        return regVal

    def stopEmulation(self, userData):
        self.enteredBlock = False
        if "visitedTargets" in userData and userData["targetVA"] not in userData["visitedTargets"]:
            userData["visitedTargets"].append(
                userData["targetVA"])
        self.uc.emu_stop()

    def resetEmuHooks(self):
        if self.uc is None:
            logging.debug(
                "resetEmuHooks: no hooks to reset, emulator has not been initialized yet")
            return
        if self.h_userhook:
            self.uc.hook_del(self.h_userhook)
            self.h_userhook = None
        if self.h_memaccesshook:
            self.uc.hook_del(self.h_memaccesshook)
            self.h_memaccesshook = None
        if self.h_codehook:
            self.uc.hook_del(self.h_codehook)
            self.h_codehook = None
        if self.h_memhook:
            self.uc.hook_del(self.h_memhook)
            self.h_memhook = None
        if self.h_inthook:
            self.uc.hook_del(self.h_inthook)
            self.h_inthook = None

    # for debugging purposes
    def getEmuState(self):
        if self.arch == unicorn.UC_ARCH_X86:
            if self.uc._mode == unicorn.UC_MODE_64:
                out = "RAX: %016X\tRBX: %016X\n" % (self.uc.reg_read(
                    unicorn.x86_const.UC_X86_REG_RAX), self.uc.reg_read(unicorn.x86_const.UC_X86_REG_RBX))
                out += "RCX: %016X\tRDX: %016X\n" % (self.uc.reg_read(
                    unicorn.x86_const.UC_X86_REG_RCX), self.uc.reg_read(unicorn.x86_const.UC_X86_REG_RDX))
                out += "RDI: %016X\tRSI: %016X\n" % (self.uc.reg_read(
                    unicorn.x86_const.UC_X86_REG_RDI), self.uc.reg_read(unicorn.x86_const.UC_X86_REG_RSI))
                out += "R8: %016X\tR9: %016X\n" % (self.uc.reg_read(
                    unicorn.x86_const.UC_X86_REG_R8), self.uc.reg_read(unicorn.x86_const.UC_X86_REG_R9))
                out += "RBP: %016X\tRSP: %016X\n" % (self.uc.reg_read(
                    unicorn.x86_const.UC_X86_REG_RBP), self.uc.reg_read(unicorn.x86_const.UC_X86_REG_RSP))
                out += "RIP: %016X\n" % (self.uc.reg_read(unicorn.x86_const.UC_X86_REG_RIP))
            elif self.uc._mode == unicorn.UC_MODE_32:
                out = "EAX: %016X\tEBX: %016X\n" % (self.uc.reg_read(
                    unicorn.x86_const.UC_X86_REG_EAX), self.uc.reg_read(unicorn.x86_const.UC_X86_REG_EBX))
                out += "ECX: %016X\tEDX: %016X\n" % (self.uc.reg_read(
                    unicorn.x86_const.UC_X86_REG_ECX), self.uc.reg_read(unicorn.x86_const.UC_X86_REG_EDX))
                out += "EDI: %016X\tESI: %016X\n" % (self.uc.reg_read(
                    unicorn.x86_const.UC_X86_REG_EDI), self.uc.reg_read(unicorn.x86_const.UC_X86_REG_ESI))
                out += "EBP: %016X\tESP: %016X\n" % (self.uc.reg_read(
                    unicorn.x86_const.UC_X86_REG_EBP), self.uc.reg_read(unicorn.x86_const.UC_X86_REG_ESP))
                out += "EIP: %016X\n" % (self.uc.reg_read(unicorn.x86_const.UC_X86_REG_EIP))
        elif self.arch == unicorn.UC_ARCH_ARM64:
            out = "X0: %016X\tX1: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X0), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X1))
            out += "X2: %016X\tX3: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X2), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X3))
            out += "X4: %016X\tX5: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X4), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X5))
            out += "X6: %016X\tX7: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X6), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X7))
            out += "X8: %016X\tX9: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X8), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X9))
            out += "X10: %016X\tX11: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X10), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X11))
            out += "X12: %016X\tX13: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X12), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X13))
            out += "X14: %016X\tX15: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X14), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X15))
            out += "X16: %016X\tX17: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X16), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X17))
            out += "X18: %016X\tX19: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X18), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X19))
            out += "X20: %016X\tX21: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X20), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X21))
            out += "X22: %016X\tX23: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X22), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X23))
            out += "X24: %016X\tX25: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X24), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X25))
            out += "X26: %016X\tX27: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X26), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X27))
            out += "X28: %016X\tX29: %016X\n" % (self.uc.reg_read(
                unicorn.arm64_const.UC_ARM64_REG_X28), self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X29))
            out += "X30: %016X\n" % (self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_X30))
            out += "PC: %016X\n" % (self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_PC))
            out += "SP: %016X\n" % (self.uc.reg_read(unicorn.arm64_const.UC_ARM64_REG_SP))
        elif self.arch == unicorn.UC_ARCH_ARM:
            out = "R0: %08X\tR1: %08X\n" % (self.uc.reg_read(
                unicorn.arm_const.UC_ARM_REG_R0), self.uc.reg_read(unicorn.arm_const.UC_ARM_REG_R1))
            out += "R2: %08X\tR3: %08X\n" % (self.uc.reg_read(
                unicorn.arm_const.UC_ARM_REG_R2), self.uc.reg_read(unicorn.arm_const.UC_ARM_REG_R3))
            out += "R4: %08X\tR5: %08X\n" % (self.uc.reg_read(
                unicorn.arm_const.UC_ARM_REG_R4), self.uc.reg_read(unicorn.arm_const.UC_ARM_REG_R5))
            out += "R6: %08X\tR7: %08X\n" % (self.uc.reg_read(
                unicorn.arm_const.UC_ARM_REG_R6), self.uc.reg_read(unicorn.arm_const.UC_ARM_REG_R7))
            out += "R8: %08X\tR9: %08X\n" % (self.uc.reg_read(
                unicorn.arm_const.UC_ARM_REG_R8), self.uc.reg_read(unicorn.arm_const.UC_ARM_REG_R9))
            out += "R10: %08X\tR11: %08X\n" % (self.uc.reg_read(
                unicorn.arm_const.UC_ARM_REG_R10), self.uc.reg_read(unicorn.arm_const.UC_ARM_REG_R11))
            out += "R12: %08X\tR13: %08X\n" % (self.uc.reg_read(
                unicorn.arm_const.UC_ARM_REG_R12), self.uc.reg_read(unicorn.arm_const.UC_ARM_REG_R13))
            out += "R14: %08X\tR15: %08X\n" % (self.uc.reg_read(
                unicorn.arm_const.UC_ARM_REG_R14), self.uc.reg_read(unicorn.arm_const.UC_ARM_REG_R15))
            out += "PC: %08X\n" % self.uc.reg_read(unicorn.arm_const.UC_ARM_REG_R15)
            out += "SP: %08X\n" % self.uc.reg_read(unicorn.arm_const.UC_ARM_REG_R13)
        else:
            return ""
        return out

    # returns null-terminated string of bytes from the emulator's memory, starting at addr, do not necessarily need
    # to be printable characters
    def getEmuString(self, addr):
        out = ""
        while str(self.uc.mem_read(addr, 1)) != "\x00":
            out += str(self.uc.mem_read(addr, 1))
            addr += 1
        return out
    
    def getEmuWideString(self, addr):
        out = ""
        while str(self.uc.mem_read(addr, 2)) != "\x00\x00":
            out += str(self.uc.mem_read(addr, 2))
            addr += 2
        return out

    # returns a <size> string of bytes read from <addr>
    def getEmuBytes(self, addr, size):
        return str(self.uc.mem_read(addr, size))

    # reads pointer value in emulator's memory
    def getEmuPtr(self, va):
        return struct.unpack(self.pack_fmt, self.uc.mem_read(va, self.size_pointer))[0]
        
    # writes a pointer value in emulator's memory
    def writeEmuPtr(self, va, value):
        self.uc.mem_write(va, struct.pack(self.pack_fmt, value))

    # for debugging
    def formatBB(self, bb):
        bbtype = {0: "fcb_normal", 1: "idaapi.fcb_indjump", 2: "idaapi.fcb_ret", 3: "fcb_cndret",
                  4: "idaapi.fcb_noret", 5: "fcb_enoret", 6: "idaapi.fcb_extern", 7: "fcb_error"}
        return("ID: %d, Start: 0x%x, End: 0x%x, Last instruction: 0x%x, Size: %d, "
               "Type: %s" % (bb.id, bb.start_ea, bb.end_ea, idc.idc.prev_head(bb.end_ea,
                             idc.get_inf_attr(idc.INF_MIN_EA)), (bb.end_ea - bb.start_ea), bbtype[bb.type]))

    def getSegSize(self, ea, segEnd):
        size = 0
        while idc.has_value(idc.get_full_flags(ea)):
            if ea >= segEnd:
                break
            size += 1
            ea += 1
        return size

    # returns True if ea is in an area designated by IDA to be in thumb mode
    def isThumbMode(self, ea):
        return idc.get_sreg(ea, "T") == 1

    def pageAlign(self, addr):
        return addr & 0xfffffffffffff000

    # uses depth first searching on IDA's idaapi.FlowChart in a given target's function to find up to maxPaths possible
    # ways to target from function start
    # this will return an empty list if there are no branches in function
    # returns IDA's idaapi.FlowChart object, converted to a Python list of tuples, as well as a list of lists
    # containing paths to targets in the form of basic block IDs
    def getPaths(self, targetVA, maxPaths):
        function = idaapi.get_func(targetVA)
        flowchart = idaapi.FlowChart(function)
        target_bb = self.getBlockIdByVA(targetVA, flowchart)
        if function.start_ea in self.paths:
            paths = self.paths[function.start_ea]
        else:
            start_bb = self.getStartBB(function, flowchart)
            logging.debug("exploring function with %d blocks" % flowchart.size)
            self._explore(start_bb)
            self.explorePaths.pop()
            paths = deepcopy(self.explorePaths)
            del(self.explorePaths)
            self.paths[function.start_ea] = paths
        targetPaths = []
        for p in paths:
            if target_bb in p:
                targetPaths.append(p)

        # truncate paths to target bb
        for i in range(len(targetPaths)):
            targetPaths[i] = targetPaths[i][:targetPaths[i].index(
                target_bb) + 1]

        # unique list of paths
        uniqTargetPaths = []
        for p in targetPaths:
            if p not in uniqTargetPaths:
                uniqTargetPaths.append(p)
        uniqTargetPaths = uniqTargetPaths[:maxPaths]
        logging.debug("code paths to target: %s" % repr(uniqTargetPaths))

        # create my own idaapi.FlowChart object so it can be pickled for debugging purposes
        flow = {}
        for bb in flowchart:
            flow[bb.id] = (bb.start_ea, bb.end_ea)
        return flow, uniqTargetPaths

    # same as getPaths, but only get first path to target found during exploration
    def getPath(self, targetVA):
        function = idaapi.get_func(targetVA)
        flowchart = idaapi.FlowChart(function)
        target_bb = self.getBlockIdByVA(targetVA, flowchart)
        start_bb = self.getStartBB(function, flowchart)
        if self.verbose > 0:
            logging.debug("exploring function with %d blocks" % flowchart.size)
        idx = self._explore(start_bb, target_bb)
        if idx is None:
            logging.debug(
                "path to target %s could not be found, skipping" % self.hexString(targetVA))
            return None, None

        path = deepcopy(self.explorePaths[idx])
        del(self.explorePaths)
        if self.verbose > 0:
            logging.debug("code path to target: %s" % repr(path))

        # create my own idaapi.FlowChart object so it can be pickled for debugging purposes
        flow = {}
        for bb in flowchart:
            flow[bb.id] = (bb.start_ea, bb.end_ea)
        return flow, [path]

    def getStartBB(self, function, flowchart):
        for bb in flowchart:
            if bb.start_ea == function.start_ea:
                return bb

    def getBlockIdByVA(self, targetVA, flowchart):
        return self.getBlockByVA(targetVA, flowchart).id

    def getBlockByVA(self, targetVA, flowchart):
        for bb in flowchart:
            if targetVA >= bb.start_ea and targetVA < bb.end_ea:
                return bb

    def isTerminatingBB(self, bb):
        if (bb.type == idaapi.fcb_ret or bb.type == idaapi.fcb_noret or
                (bb.type == idaapi.fcb_indjump and len(list(bb.succs())) == 0)):
            return True
        for b in bb.succs():
            if b.type == idaapi.fcb_extern:
                return True

        return False

    # sets up arch/mode specific variables, initializes emulator
    def initEmuHelper(self):
        info = idaapi.get_inf_structure()
        if info.procName == "metapc":
            self.arch = unicorn.UC_ARCH_X86
            arch = "X86"
            if info.is_64bit():
                self.mode = unicorn.UC_MODE_64
                self.derefPtr = idc.get_qword
                mode = "64-bit"
                self.size_pointer = 8
                self.pack_fmt = "<Q"
                self.pageMask = 0xfffffffffffff000
                self.regs = {"ax": unicorn.x86_const.UC_X86_REG_RAX, "bx": unicorn.x86_const.UC_X86_REG_RBX,
                             "cx": unicorn.x86_const.UC_X86_REG_RCX, "dx": unicorn.x86_const.UC_X86_REG_RDX,
                             "di": unicorn.x86_const.UC_X86_REG_RDI, "si": unicorn.x86_const.UC_X86_REG_RSI,
                             "bp": unicorn.x86_const.UC_X86_REG_RBP, "sp": unicorn.x86_const.UC_X86_REG_RSP,
                             "ip": unicorn.x86_const.UC_X86_REG_RIP, "pc": unicorn.x86_const.UC_X86_REG_RIP,
                             "rax": unicorn.x86_const.UC_X86_REG_RAX, "rbx": unicorn.x86_const.UC_X86_REG_RBX,
                             "rcx": unicorn.x86_const.UC_X86_REG_RCX, "rdx": unicorn.x86_const.UC_X86_REG_RDX,
                             "rdi": unicorn.x86_const.UC_X86_REG_RDI, "rsi": unicorn.x86_const.UC_X86_REG_RSI,
                             "rbp": unicorn.x86_const.UC_X86_REG_RBP, "rsp": unicorn.x86_const.UC_X86_REG_RSP,
                             "r8": unicorn.x86_const.UC_X86_REG_R8, "r9": unicorn.x86_const.UC_X86_REG_R9,
                             "r10": unicorn.x86_const.UC_X86_REG_R10, "r11": unicorn.x86_const.UC_X86_REG_R11,
                             "r12": unicorn.x86_const.UC_X86_REG_R12, "r13": unicorn.x86_const.UC_X86_REG_R13,
                             "r14": unicorn.x86_const.UC_X86_REG_R14, "r15": unicorn.x86_const.UC_X86_REG_R15,
                             "ret": unicorn.x86_const.UC_X86_REG_RAX}
                if info.filetype == 11:
                    self.filetype = "PE"
                    self.tilName = "mssdk_win7"
                    self.regs.update({"arg1": unicorn.x86_const.UC_X86_REG_RCX,
                                      "arg2": unicorn.x86_const.UC_X86_REG_RDX,
                                      "arg3": unicorn.x86_const.UC_X86_REG_R8,
                                      "arg4": unicorn.x86_const.UC_X86_REG_R9})
                elif info.filetype == 25:
                    self.filetype = "MACHO"
                    self.tilName = "macosx64"
                    self.regs.update({"arg1": unicorn.x86_const.UC_X86_REG_RDI,
                                      "arg2": unicorn.x86_const.UC_X86_REG_RSI,
                                      "arg3": unicorn.x86_const.UC_X86_REG_RDX,
                                      "arg4": unicorn.x86_const.UC_X86_REG_RCX})
                elif info.filetype == 18:
                    self.filetype = "ELF"
                    self.tilName = "gnulnx_x64"
                    self.regs.update({"arg1": unicorn.x86_const.UC_X86_REG_RDI,
                                      "arg2": unicorn.x86_const.UC_X86_REG_RSI,
                                      "arg3": unicorn.x86_const.UC_X86_REG_RDX,
                                      "arg4": unicorn.x86_const.UC_X86_REG_RCX})
                else:
                    self.filetype = "UNKNOWN"
                    # assume PE for mem dumps
                    self.regs.update({"arg1": unicorn.x86_const.UC_X86_REG_RCX,
                                      "arg2": unicorn.x86_const.UC_X86_REG_RDX,
                                      "arg3": unicorn.x86_const.UC_X86_REG_R8,
                                      "arg4": unicorn.x86_const.UC_X86_REG_R9})
            elif info.is_32bit():
                if info.filetype == 11:
                    self.filetype = "PE"
                    self.tilName = "mssdk"
                elif info.filetype == 25:
                    self.filetype = "MACHO"
                    self.tilName = "macosx"
                elif info.filetype == 18:
                    self.filetype = "ELF"
                    self.tilName = "gnulnx_x86"
                else:
                    self.filetype = "UNKNOWN"
                self.mode = unicorn.UC_MODE_32
                self.derefPtr = idc.get_wide_dword
                mode = "32-bit"
                self.size_pointer = 4
                self.pack_fmt = "<I"
                self.pageMask = 0xfffff000
                self.regs = {"ax": unicorn.x86_const.UC_X86_REG_EAX, "bx": unicorn.x86_const.UC_X86_REG_EBX,
                             "cx": unicorn.x86_const.UC_X86_REG_ECX, "dx": unicorn.x86_const.UC_X86_REG_EDX,
                             "di": unicorn.x86_const.UC_X86_REG_EDI, "si": unicorn.x86_const.UC_X86_REG_ESI,
                             "bp": unicorn.x86_const.UC_X86_REG_EBP, "sp": unicorn.x86_const.UC_X86_REG_ESP,
                             "ip": unicorn.x86_const.UC_X86_REG_EIP, "pc": unicorn.x86_const.UC_X86_REG_EIP,
                             "eax": unicorn.x86_const.UC_X86_REG_EAX, "ebx": unicorn.x86_const.UC_X86_REG_EBX,
                             "ecx": unicorn.x86_const.UC_X86_REG_ECX, "edx": unicorn.x86_const.UC_X86_REG_EDX,
                             "edi": unicorn.x86_const.UC_X86_REG_EDI, "esi": unicorn.x86_const.UC_X86_REG_ESI,
                             "ebp": unicorn.x86_const.UC_X86_REG_EBP, "esp": unicorn.x86_const.UC_X86_REG_ESP,
                             "ret": unicorn.x86_const.UC_X86_REG_EAX}
            
            else:
                logging.debug(
                    "sample contains code for unsupported processor architecture")
                return
        elif info.procName == "ARM":
            self.mode = unicorn.UC_MODE_ARM
            mode = "ARM"
            if info.is_64bit():
                self.arch = unicorn.UC_ARCH_ARM64
                arch = "ARM64"
                if info.filetype == 11:
                    self.filetype = "PE"
                    self.tilName = "mssdk_win7"
                elif info.filetype == 25:
                    self.filetype = "MACHO"
                    self.tilName = "macosx64"
                elif info.filetype == 18:
                    self.filetype = "ELF"
                    self.tilName = "gnulnx_x64"
                else:
                    self.filetype = "UNKNOWN"
                self.size_pointer = 8
                self.pack_fmt = "<Q"
                self.derefPtr = idc.get_qword
                self.pageMask = 0xfffffffffffff000
                self.regs = {"R0": unicorn.arm64_const.UC_ARM64_REG_X0, "R1": unicorn.arm64_const.UC_ARM64_REG_X1,
                             "R2": unicorn.arm64_const.UC_ARM64_REG_X2, "R3": unicorn.arm64_const.UC_ARM64_REG_X3,
                             "R4": unicorn.arm64_const.UC_ARM64_REG_X4, "R5": unicorn.arm64_const.UC_ARM64_REG_X5,
                             "R6": unicorn.arm64_const.UC_ARM64_REG_X6, "R7": unicorn.arm64_const.UC_ARM64_REG_X7,
                             "R8": unicorn.arm64_const.UC_ARM64_REG_X8, "R9": unicorn.arm64_const.UC_ARM64_REG_X9,
                             "R10": unicorn.arm64_const.UC_ARM64_REG_X10, "R11": unicorn.arm64_const.UC_ARM64_REG_X11,
                             "R12": unicorn.arm64_const.UC_ARM64_REG_X12, "R13": unicorn.arm64_const.UC_ARM64_REG_X13,
                             "R14": unicorn.arm64_const.UC_ARM64_REG_X14, "R15": unicorn.arm64_const.UC_ARM64_REG_X15,
                             "X0": unicorn.arm64_const.UC_ARM64_REG_X0, "X1": unicorn.arm64_const.UC_ARM64_REG_X1,
                             "X2": unicorn.arm64_const.UC_ARM64_REG_X2, "X3": unicorn.arm64_const.UC_ARM64_REG_X3,
                             "X4": unicorn.arm64_const.UC_ARM64_REG_X4, "X5": unicorn.arm64_const.UC_ARM64_REG_X5,
                             "X6": unicorn.arm64_const.UC_ARM64_REG_X6, "X7": unicorn.arm64_const.UC_ARM64_REG_X7,
                             "X8": unicorn.arm64_const.UC_ARM64_REG_X8, "X9": unicorn.arm64_const.UC_ARM64_REG_X9,
                             "X10": unicorn.arm64_const.UC_ARM64_REG_X10, "X11": unicorn.arm64_const.UC_ARM64_REG_X11,
                             "X12": unicorn.arm64_const.UC_ARM64_REG_X12, "X13": unicorn.arm64_const.UC_ARM64_REG_X13,
                             "X14": unicorn.arm64_const.UC_ARM64_REG_X14, "X15": unicorn.arm64_const.UC_ARM64_REG_X15,
                             "X16": unicorn.arm64_const.UC_ARM64_REG_X16, "X17": unicorn.arm64_const.UC_ARM64_REG_X17,
                             "X18": unicorn.arm64_const.UC_ARM64_REG_X18, "X19": unicorn.arm64_const.UC_ARM64_REG_X19,
                             "X20": unicorn.arm64_const.UC_ARM64_REG_X20, "X21": unicorn.arm64_const.UC_ARM64_REG_X21,
                             "X22": unicorn.arm64_const.UC_ARM64_REG_X22, "X23": unicorn.arm64_const.UC_ARM64_REG_X23,
                             "X24": unicorn.arm64_const.UC_ARM64_REG_X24, "X25": unicorn.arm64_const.UC_ARM64_REG_X25,
                             "X26": unicorn.arm64_const.UC_ARM64_REG_X26, "X27": unicorn.arm64_const.UC_ARM64_REG_X27,
                             "X28": unicorn.arm64_const.UC_ARM64_REG_X28, "X29": unicorn.arm64_const.UC_ARM64_REG_X29,
                             "X30": unicorn.arm64_const.UC_ARM64_REG_X30, "W0": unicorn.arm64_const.UC_ARM64_REG_X0,
                             "W1": unicorn.arm64_const.UC_ARM64_REG_X1, "W2": unicorn.arm64_const.UC_ARM64_REG_X2,
                             "W3": unicorn.arm64_const.UC_ARM64_REG_X3, "W4": unicorn.arm64_const.UC_ARM64_REG_X4,
                             "W5": unicorn.arm64_const.UC_ARM64_REG_X5, "W6": unicorn.arm64_const.UC_ARM64_REG_X6,
                             "W7": unicorn.arm64_const.UC_ARM64_REG_X7, "W8": unicorn.arm64_const.UC_ARM64_REG_X8,
                             "W9": unicorn.arm64_const.UC_ARM64_REG_X9, "W10": unicorn.arm64_const.UC_ARM64_REG_X10,
                             "W11": unicorn.arm64_const.UC_ARM64_REG_X11, "W12": unicorn.arm64_const.UC_ARM64_REG_X12,
                             "W13": unicorn.arm64_const.UC_ARM64_REG_X13, "W14": unicorn.arm64_const.UC_ARM64_REG_X14,
                             "W15": unicorn.arm64_const.UC_ARM64_REG_X15, "W16": unicorn.arm64_const.UC_ARM64_REG_X16,
                             "W17": unicorn.arm64_const.UC_ARM64_REG_X17, "W18": unicorn.arm64_const.UC_ARM64_REG_X18,
                             "W19": unicorn.arm64_const.UC_ARM64_REG_X19, "W20": unicorn.arm64_const.UC_ARM64_REG_X20,
                             "W21": unicorn.arm64_const.UC_ARM64_REG_X21, "W22": unicorn.arm64_const.UC_ARM64_REG_X22,
                             "W23": unicorn.arm64_const.UC_ARM64_REG_X23, "W24": unicorn.arm64_const.UC_ARM64_REG_X24,
                             "W25": unicorn.arm64_const.UC_ARM64_REG_X25, "W26": unicorn.arm64_const.UC_ARM64_REG_X26,
                             "W27": unicorn.arm64_const.UC_ARM64_REG_X27, "W28": unicorn.arm64_const.UC_ARM64_REG_X28,
                             "W29": unicorn.arm64_const.UC_ARM64_REG_X29, "W30": unicorn.arm64_const.UC_ARM64_REG_X30,
                             "PC": unicorn.arm64_const.UC_ARM64_REG_PC, "pc": unicorn.arm64_const.UC_ARM64_REG_PC,
                             "LR": unicorn.arm64_const.UC_ARM64_REG_X30, "SP": unicorn.arm64_const.UC_ARM64_REG_SP,
                             "sp": unicorn.arm64_const.UC_ARM64_REG_SP, "ret": unicorn.arm64_const.UC_ARM64_REG_X0,
                             "S0": unicorn.arm64_const.UC_ARM64_REG_S0, "S1": unicorn.arm64_const.UC_ARM64_REG_S1,
                             "S2": unicorn.arm64_const.UC_ARM64_REG_S2, "S3": unicorn.arm64_const.UC_ARM64_REG_S3,
                             "S4": unicorn.arm64_const.UC_ARM64_REG_S4, "S5": unicorn.arm64_const.UC_ARM64_REG_S5,
                             "S6": unicorn.arm64_const.UC_ARM64_REG_S6, "S7": unicorn.arm64_const.UC_ARM64_REG_S7,
                             "S8": unicorn.arm64_const.UC_ARM64_REG_S8, "S9": unicorn.arm64_const.UC_ARM64_REG_S9,
                             "S10": unicorn.arm64_const.UC_ARM64_REG_S10, "S11": unicorn.arm64_const.UC_ARM64_REG_S11,
                             "S12": unicorn.arm64_const.UC_ARM64_REG_S12, "S13": unicorn.arm64_const.UC_ARM64_REG_S13,
                             "S14": unicorn.arm64_const.UC_ARM64_REG_S14, "S15": unicorn.arm64_const.UC_ARM64_REG_S15,
                             "S16": unicorn.arm64_const.UC_ARM64_REG_S16, "S17": unicorn.arm64_const.UC_ARM64_REG_S17,
                             "S18": unicorn.arm64_const.UC_ARM64_REG_S18, "S19": unicorn.arm64_const.UC_ARM64_REG_S19,
                             "S20": unicorn.arm64_const.UC_ARM64_REG_S20, "S21": unicorn.arm64_const.UC_ARM64_REG_S21,
                             "S22": unicorn.arm64_const.UC_ARM64_REG_S22, "S23": unicorn.arm64_const.UC_ARM64_REG_S23,
                             "S24": unicorn.arm64_const.UC_ARM64_REG_S24, "S25": unicorn.arm64_const.UC_ARM64_REG_S25,
                             "S26": unicorn.arm64_const.UC_ARM64_REG_S26, "S27": unicorn.arm64_const.UC_ARM64_REG_S27,
                             "S28": unicorn.arm64_const.UC_ARM64_REG_S28, "S29": unicorn.arm64_const.UC_ARM64_REG_S29,
                             "S30": unicorn.arm64_const.UC_ARM64_REG_S30, "S31": unicorn.arm64_const.UC_ARM64_REG_S31,
                             "D0": unicorn.arm64_const.UC_ARM64_REG_D0, "D1": unicorn.arm64_const.UC_ARM64_REG_D1,
                             "D2": unicorn.arm64_const.UC_ARM64_REG_D2, "D3": unicorn.arm64_const.UC_ARM64_REG_D3,
                             "D4": unicorn.arm64_const.UC_ARM64_REG_D4, "D5": unicorn.arm64_const.UC_ARM64_REG_D5,
                             "D6": unicorn.arm64_const.UC_ARM64_REG_D6, "D7": unicorn.arm64_const.UC_ARM64_REG_D7,
                             "D8": unicorn.arm64_const.UC_ARM64_REG_D8, "D9": unicorn.arm64_const.UC_ARM64_REG_D9,
                             "D10": unicorn.arm64_const.UC_ARM64_REG_D10, "D11": unicorn.arm64_const.UC_ARM64_REG_D11,
                             "D12": unicorn.arm64_const.UC_ARM64_REG_D12, "D13": unicorn.arm64_const.UC_ARM64_REG_D13,
                             "D14": unicorn.arm64_const.UC_ARM64_REG_D14, "D15": unicorn.arm64_const.UC_ARM64_REG_D15,
                             "D16": unicorn.arm64_const.UC_ARM64_REG_D16, "D17": unicorn.arm64_const.UC_ARM64_REG_D17,
                             "D18": unicorn.arm64_const.UC_ARM64_REG_D18, "D19": unicorn.arm64_const.UC_ARM64_REG_D19,
                             "D20": unicorn.arm64_const.UC_ARM64_REG_D20, "D21": unicorn.arm64_const.UC_ARM64_REG_D21,
                             "D22": unicorn.arm64_const.UC_ARM64_REG_D22, "D23": unicorn.arm64_const.UC_ARM64_REG_D23,
                             "D24": unicorn.arm64_const.UC_ARM64_REG_D24, "D25": unicorn.arm64_const.UC_ARM64_REG_D25,
                             "D26": unicorn.arm64_const.UC_ARM64_REG_D26, "D27": unicorn.arm64_const.UC_ARM64_REG_D27,
                             "D28": unicorn.arm64_const.UC_ARM64_REG_D28, "D29": unicorn.arm64_const.UC_ARM64_REG_D29,
                             "D30": unicorn.arm64_const.UC_ARM64_REG_D30, "D31": unicorn.arm64_const.UC_ARM64_REG_D31,
                             "H0": unicorn.arm64_const.UC_ARM64_REG_H0, "H1": unicorn.arm64_const.UC_ARM64_REG_H1,
                             "H2": unicorn.arm64_const.UC_ARM64_REG_H2, "H3": unicorn.arm64_const.UC_ARM64_REG_H3,
                             "H4": unicorn.arm64_const.UC_ARM64_REG_H4, "H5": unicorn.arm64_const.UC_ARM64_REG_H5,
                             "H6": unicorn.arm64_const.UC_ARM64_REG_H6, "H7": unicorn.arm64_const.UC_ARM64_REG_H7,
                             "H8": unicorn.arm64_const.UC_ARM64_REG_H8, "H9": unicorn.arm64_const.UC_ARM64_REG_H9,
                             "H10": unicorn.arm64_const.UC_ARM64_REG_H10, "H11": unicorn.arm64_const.UC_ARM64_REG_H11,
                             "H12": unicorn.arm64_const.UC_ARM64_REG_H12, "H13": unicorn.arm64_const.UC_ARM64_REG_H13,
                             "H14": unicorn.arm64_const.UC_ARM64_REG_H14, "H15": unicorn.arm64_const.UC_ARM64_REG_H15,
                             "H16": unicorn.arm64_const.UC_ARM64_REG_H16, "H17": unicorn.arm64_const.UC_ARM64_REG_H17,
                             "H18": unicorn.arm64_const.UC_ARM64_REG_H18, "H19": unicorn.arm64_const.UC_ARM64_REG_H19,
                             "H20": unicorn.arm64_const.UC_ARM64_REG_H20, "H21": unicorn.arm64_const.UC_ARM64_REG_H21,
                             "H22": unicorn.arm64_const.UC_ARM64_REG_H22, "H23": unicorn.arm64_const.UC_ARM64_REG_H23,
                             "H24": unicorn.arm64_const.UC_ARM64_REG_H24, "H25": unicorn.arm64_const.UC_ARM64_REG_H25,
                             "H26": unicorn.arm64_const.UC_ARM64_REG_H26, "H27": unicorn.arm64_const.UC_ARM64_REG_H27,
                             "H28": unicorn.arm64_const.UC_ARM64_REG_H28, "H29": unicorn.arm64_const.UC_ARM64_REG_H29,
                             "H30": unicorn.arm64_const.UC_ARM64_REG_H30, "H31": unicorn.arm64_const.UC_ARM64_REG_H31,
                             "Q0": unicorn.arm64_const.UC_ARM64_REG_Q0, "Q1": unicorn.arm64_const.UC_ARM64_REG_Q1,
                             "Q2": unicorn.arm64_const.UC_ARM64_REG_Q2, "Q3": unicorn.arm64_const.UC_ARM64_REG_Q3,
                             "Q4": unicorn.arm64_const.UC_ARM64_REG_Q4, "Q5": unicorn.arm64_const.UC_ARM64_REG_Q5,
                             "Q6": unicorn.arm64_const.UC_ARM64_REG_Q6, "Q7": unicorn.arm64_const.UC_ARM64_REG_Q7,
                             "Q8": unicorn.arm64_const.UC_ARM64_REG_Q8, "Q9": unicorn.arm64_const.UC_ARM64_REG_Q9,
                             "Q10": unicorn.arm64_const.UC_ARM64_REG_Q10, "Q11": unicorn.arm64_const.UC_ARM64_REG_Q11,
                             "Q12": unicorn.arm64_const.UC_ARM64_REG_Q12, "Q13": unicorn.arm64_const.UC_ARM64_REG_Q13,
                             "Q14": unicorn.arm64_const.UC_ARM64_REG_Q14, "Q15": unicorn.arm64_const.UC_ARM64_REG_Q15,
                             "Q16": unicorn.arm64_const.UC_ARM64_REG_Q16, "Q17": unicorn.arm64_const.UC_ARM64_REG_Q17,
                             "Q18": unicorn.arm64_const.UC_ARM64_REG_Q18, "Q19": unicorn.arm64_const.UC_ARM64_REG_Q19,
                             "Q20": unicorn.arm64_const.UC_ARM64_REG_Q20, "Q21": unicorn.arm64_const.UC_ARM64_REG_Q21,
                             "Q22": unicorn.arm64_const.UC_ARM64_REG_Q22, "Q23": unicorn.arm64_const.UC_ARM64_REG_Q23,
                             "Q24": unicorn.arm64_const.UC_ARM64_REG_Q24, "Q25": unicorn.arm64_const.UC_ARM64_REG_Q25,
                             "Q26": unicorn.arm64_const.UC_ARM64_REG_Q26, "Q27": unicorn.arm64_const.UC_ARM64_REG_Q27,
                             "Q28": unicorn.arm64_const.UC_ARM64_REG_Q28, "Q29": unicorn.arm64_const.UC_ARM64_REG_Q29,
                             "Q30": unicorn.arm64_const.UC_ARM64_REG_Q30, "Q31": unicorn.arm64_const.UC_ARM64_REG_Q31}
                self.regs.update({"arg1": unicorn.arm64_const.UC_ARM64_REG_X0,
                                  "arg2": unicorn.arm64_const.UC_ARM64_REG_X1,
                                  "arg3": unicorn.arm64_const.UC_ARM64_REG_X2,
                                  "arg4": unicorn.arm64_const.UC_ARM64_REG_X3})
            elif info.is_32bit():
                self.arch = unicorn.UC_ARCH_ARM
                arch = "ARM"
                if info.filetype == 11:
                    self.filetype = "PE"
                    self.tilName = "mssdk"
                elif info.filetype == 25:
                    self.filetype = "MACHO"
                    self.tilName = "macosx"
                elif info.filetype == 18:
                    self.filetype = "ELF"
                    self.tilName = "gnulnx_x86"
                else:
                    self.filetype = "UNKNOWN"
                self.size_pointer = 4
                self.pack_fmt = "<I"
                self.derefPtr = idc.get_wide_dword
                self.pageMask = 0xfffff000
                self.regs = {"R0": unicorn.arm_const.UC_ARM_REG_R0, "R1": unicorn.arm_const.UC_ARM_REG_R1,
                             "R2": unicorn.arm_const.UC_ARM_REG_R2, "R3": unicorn.arm_const.UC_ARM_REG_R3,
                             "R4": unicorn.arm_const.UC_ARM_REG_R4, "R5": unicorn.arm_const.UC_ARM_REG_R5,
                             "R6": unicorn.arm_const.UC_ARM_REG_R6, "R7": unicorn.arm_const.UC_ARM_REG_R7,
                             "R8": unicorn.arm_const.UC_ARM_REG_R8, "R9": unicorn.arm_const.UC_ARM_REG_R9,
                             "R10": unicorn.arm_const.UC_ARM_REG_R10, "R11": unicorn.arm_const.UC_ARM_REG_R11,
                             "R12": unicorn.arm_const.UC_ARM_REG_R12, "R13": unicorn.arm_const.UC_ARM_REG_R13,
                             "R14": unicorn.arm_const.UC_ARM_REG_R14, "R15": unicorn.arm_const.UC_ARM_REG_R15,
                             "PC": unicorn.arm_const.UC_ARM_REG_R15, "pc": unicorn.arm_const.UC_ARM_REG_R15,
                             "LR": unicorn.arm_const.UC_ARM_REG_R14, "SP": unicorn.arm_const.UC_ARM_REG_R13,
                             "sp": unicorn.arm_const.UC_ARM_REG_R13, "apsr": unicorn.arm_const.UC_ARM_REG_APSR,
                             "APSR": unicorn.arm_const.UC_ARM_REG_APSR, "ret": unicorn.arm_const.UC_ARM_REG_R0,
                             "S0": unicorn.arm_const.UC_ARM_REG_S0, "S1": unicorn.arm_const.UC_ARM_REG_S1,
                             "S2": unicorn.arm_const.UC_ARM_REG_S2, "S3": unicorn.arm_const.UC_ARM_REG_S3,
                             "S4": unicorn.arm_const.UC_ARM_REG_S4, "S5": unicorn.arm_const.UC_ARM_REG_S5,
                             "S6": unicorn.arm_const.UC_ARM_REG_S6, "S7": unicorn.arm_const.UC_ARM_REG_S7,
                             "S8": unicorn.arm_const.UC_ARM_REG_S8, "S9": unicorn.arm_const.UC_ARM_REG_S9,
                             "S10": unicorn.arm_const.UC_ARM_REG_S10, "S11": unicorn.arm_const.UC_ARM_REG_S11,
                             "S12": unicorn.arm_const.UC_ARM_REG_S12, "S13": unicorn.arm_const.UC_ARM_REG_S13,
                             "S14": unicorn.arm_const.UC_ARM_REG_S14, "S15": unicorn.arm_const.UC_ARM_REG_S15,
                             "S16": unicorn.arm_const.UC_ARM_REG_S16, "S17": unicorn.arm_const.UC_ARM_REG_S17,
                             "S18": unicorn.arm_const.UC_ARM_REG_S18, "S19": unicorn.arm_const.UC_ARM_REG_S19,
                             "S20": unicorn.arm_const.UC_ARM_REG_S20, "S21": unicorn.arm_const.UC_ARM_REG_S21,
                             "S22": unicorn.arm_const.UC_ARM_REG_S22, "S23": unicorn.arm_const.UC_ARM_REG_S23,
                             "S24": unicorn.arm_const.UC_ARM_REG_S24, "S25": unicorn.arm_const.UC_ARM_REG_S25,
                             "S26": unicorn.arm_const.UC_ARM_REG_S26, "S27": unicorn.arm_const.UC_ARM_REG_S27,
                             "S28": unicorn.arm_const.UC_ARM_REG_S28, "S29": unicorn.arm_const.UC_ARM_REG_S29,
                             "S30": unicorn.arm_const.UC_ARM_REG_S30, "S31": unicorn.arm_const.UC_ARM_REG_S31}
                self.regs.update({"arg1": unicorn.arm_const.UC_ARM_REG_R0, "arg2": unicorn.arm_const.UC_ARM_REG_R1,
                                  "arg3": unicorn.arm_const.UC_ARM_REG_R2, "arg4": unicorn.arm_const.UC_ARM_REG_R3})
            else:
                logging.debug(
                    "sample contains code for unsupported processor architecture")
                return
        else:
            logging.debug(
                "sample contains code for unsupported processor architecture")
            return

        # naive API hooks
        self.apiHooks = {}
        self.apiHooks["GetProcessHeap"] = self._returnHandleHook
        self.apiHooks["HeapCreate"] = self._returnHandleHook
        self.apiHooks["HeapAlloc"] = self._allocMem3Hook
        self.apiHooks["HeapReAlloc"] = self._heapReAllocHook
        self.apiHooks["RtlAllocateHeap"] = self._allocMem3Hook
        self.apiHooks["AllocateHeap"] = self._allocMem1Hook
        
        # ignore LMEM_MOVEABLE flag, return mem ptr anyway, have Lock return ptr param
        self.apiHooks["LocalAlloc"] = self._allocMem2Hook
        self.apiHooks["LocalLock"] = self._returnParam1Hook
        self.apiHooks["GlobalAlloc"] = self._allocMem2Hook
        self.apiHooks["GlobalLock"] = self._returnParam1Hook
        
        # these ignore flags for now
        self.apiHooks["LocalReAlloc"] = self._reallocHook
        self.apiHooks["GlobalReAlloc"] = self._reallocHook
        
        self.apiHooks["VirtualAlloc"] = self._virtualAllocHook
        self.apiHooks["VirtualAllocEx"] = self._virtualAllocExHook
        self.apiHooks["malloc"] = self._allocMem1Hook
        self.apiHooks["calloc"] = self._callocHook
        self.apiHooks["realloc"] = self._reallocHook
        self.apiHooks["memcpy"] = self._memcpyHook
        self.apiHooks["memmove"] = self._memcpyHook
        self.apiHooks["strlen"] = self._strlenHook
        self.apiHooks["lstrlenA"] = self._strlenHook
        self.apiHooks["strnlen"] = self._strnlenHook
        self.apiHooks["strnlen_s"] = self._strnlenHook
        self.apiHooks["strcmp"] = self._strcmpHook
        self.apiHooks["lstrcmpA"] = self._strcmpHook
        self.apiHooks["strncmp"] = self._strncmpHook
        self.apiHooks["stricmp"] = self._stricmpHook
        self.apiHooks["lstrcmpiA"] = self._stricmpHook
        self.apiHooks["strnicmp"] = self._strnicmpHook
        self.apiHooks["wcscmp"] = self._wcscmpHook
        self.apiHooks["lstrcmpW"] = self._wcscmpHook
        self.apiHooks["wcsncmp"] = self._wcsncmpHook
        self.apiHooks["wcsicmp"] = self._wcsicmpHook
        self.apiHooks["lstrcmpiW"] = self._wcsicmpHook
        self.apiHooks["wcsnicmp"] = self._wcsnicmpHook
        self.apiHooks["mbscmp"] = self._strcmpHook
        self.apiHooks["mbsncmp"] = self._strncmpHook
        self.apiHooks["mbsicmp"] = self._stricmpHook
        self.apiHooks["mbsnicmp"] = self._strnicmpHook
        self.apiHooks["strcpy"] = self._strcpyHook
        self.apiHooks["strncpy"] = self._strncpyHook
        self.apiHooks["lstrcpyA"] = self._strcpyHook
        self.apiHooks["lstrcpynA"] = self._strncpyHook
        self.apiHooks["strncpy_s"] = self._strncpysHook
        self.apiHooks["wcscpy"] = self._wcscpyHook
        self.apiHooks["wcsncpy"] = self._wcsncpyHook
        self.apiHooks["lstrcpyW"] = self._wcscpyHook
        self.apiHooks["lstrcpynW"] = self._wcsncpyHook
        self.apiHooks["wcsncpy_s"] = self._wcsncpysHook
        self.apiHooks["mbscpy"] = self._strcpyHook
        self.apiHooks["mbsncpy"] = self._strncpyHook
        self.apiHooks["mbsncpy_s"] = self._strncpysHook
        self.apiHooks["memchr"] = self._memchrHook
        self.apiHooks["strchr"] = self._strchrHook
        self.apiHooks["wcschr"] = self._wcschrHook
        self.apiHooks["mbschr"] = self._strchrHook
        self.apiHooks["strrchr"] = self._strrchrHook
        self.apiHooks["wcsrchr"] = self._wcsrchrHook
        self.apiHooks["mbsrchr"] = self._strrchrHook
        self.apiHooks["wcslen"] = self._wcslenHook
        self.apiHooks["lstrlenW"] = self._wcslenHook
        self.apiHooks["mbslen"] = self._strlenHook
        self.apiHooks["mbstrlen"] = self._strlenHook
        self.apiHooks["wcsnlen"] = self._wcsnlenHook
        self.apiHooks["wcsnlen_s"] = self._wcsnlenHook
        self.apiHooks["mbsnlen"] = self._strnlenHook
        self.apiHooks["mbstrnlen"] = self._strnlenHook
        self.apiHooks["strcat"] = self._strcatHook
        self.apiHooks["lstrcatA"] = self._strcatHook
        self.apiHooks["strncat"] = self._strncatHook
        self.apiHooks["wcscat"] = self._wcscatHook
        self.apiHooks["lstrcatW"] = self._wcscatHook
        self.apiHooks["wcsncat"] = self._wcsncatHook
        self.apiHooks["mbscat"] = self._strcatHook
        self.apiHooks["mbsncat"] = self._strncatHook
        self.apiHooks["strlwr"] = self._strlwrHook
        self.apiHooks["strupr"] = self._struprHook
        self.apiHooks["wcslwr"] = self._wcslwrHook
        self.apiHooks["wcsupr"] = self._wcsuprHook
        self.apiHooks["mbslwr"] = self._strlwrHook
        self.apiHooks["mbsupr"] = self._struprHook
        self.apiHooks["strdup"] = self._strdupHook
        self.apiHooks["wcsdup"] = self._wcsdupHook
        self.apiHooks["mbsdup"] = self._strdupHook
        self.apiHooks["mbtowc"] = self._mbtowcHook
        self.apiHooks["mbstowcs"] = self._mbstowcsHook
        self.apiHooks["wctomb"] = self._wctombHook
        self.apiHooks["wcstombs"] = self._wcstombsHook
        self.apiHooks["MultiByteToWideChar"] = self._multiByteToWideCharHook
        self.apiHooks["WideCharToMultiByte"] = self._wideCharToMultiByteHook
        self.apiHooks["memset"] = self._memsetHook
        self.apiHooks["ZeroMemory"] = self._bzeroHook
        self.apiHooks["bzero"] = self._bzeroHook
        
        # builtins
        self.apiHooks["umodsi3"] = self._modHook
        
        self.allocMap = {}
        
        # Initialize emulator
        mu = unicorn.Uc(self.arch, self.mode)
        logging.debug("initialized emulator for %s with %s architecture in %s mode" % (
            self.filetype, arch, mode))
        self.uc = mu
        if self.arch == unicorn.UC_ARCH_ARM or self.arch == unicorn.UC_ARCH_ARM64:
            self._enableVFP()

    # unmap all emulator memory
    def resetEmulatorMemory(self):
        for region in self.uc.mem_regions():
            self.uc.mem_unmap(region[0], region[1] - region[0] + 1)

    def resetEmulatorHeapAndStack(self):
        for region in self.uc.mem_regions():
            if region[0] != self.baseAddr:
                self.uc.mem_unmap(region[0], region[1] - region[0] + 1)
                logging.debug("unmapped %s to %s" % (
                    self.hexString(region[0]), self.hexString(region[1])))
        self._buildStack()

    # reset emulator memory and rewrite binary segments to emulator memory, build new stack
    def reloadBinary(self):
        self.resetEmulatorMemory()
        baseAddr = idc.get_inf_attr(idc.INF_MIN_EA)
        endAddr = idc.get_inf_attr(idc.INF_MAX_EA)
        self.baseAddr = baseAddr
        memsize = endAddr - baseAddr
        memsize = self.pageAlignUp(memsize)
        # map all binary segments as one memory region for easier management
        self.uc.mem_map(baseAddr & self.pageMask, memsize)
        for segVA in idautils.Segments():
            segName = idc.get_segm_name(segVA)
            endVA = idc.get_segm_end(segVA)
            segSizeTotal = endVA - segVA
            segSize = self.getSegSize(segVA, endVA)
            logging.debug("bytes in seg: %s" % self.hexString(segSize))
            logging.debug("mapping segment %s: %s - %s" %
                          (segName, self.hexString(segVA), self.hexString(endVA)))
            if segSize > 0:
                segBytes = idc.get_bytes(segVA, segSize, False)
                self.uc.mem_write(segVA, segBytes)
            segLeftover = segSizeTotal - segSize
            if segLeftover > 0:
                self.uc.mem_write(segVA + segSize, "\x00" * segLeftover)

        self._buildStack()

    # allocs mem and writes bytes into it
    def loadBytes(self, bytes, addr=None):
        mem = self.allocEmuMem(len(bytes), addr)
        self.uc.mem_write(mem, bytes)
        return mem

    def isValidEmuPtr(self, ptr):
        for region in self.uc.mem_regions():
            if ptr >= region[0] and ptr < region[1]:
                return True
        return False
        
    def getEmuMemRegion(self, addr):
        for region in self.uc.mem_regions():
            if addr >= region[0] and addr < region[1]:
                return (region[0], region[1] + 1)
        return None
        
    # allocate emulator memory, attempts to honor specified address, otherwise begins allocations 
    # at the next page
    # aligned address above the highest, returns address, rebased if necessary
    def allocEmuMem(self, size, addr=None):
        allocSize = self.pageAlignUp(size)
        if addr is None:
            baseAddr = addr = self._findUnusedMemRegion()
        else:
            isValid = True
            baseAddr = self.pageAlign(addr)
            offs = addr - baseAddr
            for region in self.uc.mem_regions():
                # if start or end of region falls in range of a previous region
                if ((baseAddr >= region[0] and baseAddr < region[1]) or
                        (baseAddr + allocSize >= region[0] and baseAddr + allocSize < region[1])):
                    isValid = False
                    break
                # if region completely envelopes a previous region
                if baseAddr < region[0] and baseAddr + allocSize > region[1]:
                    isValid = False
                    break
            if isValid is False:
                baseAddr = self._findUnusedMemRegion()
                addr = baseAddr + offs
        logging.debug("mapping %s bytes @%s" %
                      (self.hexString(allocSize), self.hexString(baseAddr)))
        self.uc.mem_map(baseAddr, allocSize)
        return addr
     
    
    def copyEmuMem(self, dstAddr, srcAddr, size, userData):
        size = self._checkMemSize(size, userData)
        try:
            mem = str(self.uc.mem_read(srcAddr, size))
            self.uc.mem_write(dstAddr, mem)
        except Exception as e:
            logging.debug("exception in copyEmuMem @%s: %s" % (self.hexString(address), str(e)))
        
    def getCallTargetName(self, address):
        if idc.get_operand_type(address, 0) == 1:
            funcName = idc.get_name(self.uc.reg_read(
                self.regs[idc.print_operand(address, 0)]), idc.ida_name.GN_VISIBLE)
        # elif idc.get_operand_type(address, 0) == 2:
            # funcName = idc.get_name(self.getEmuPtr(idc.get_operand_value(address, 0)), 
            #                         idc.ida_name.GN_VISIBLE)
        else:
            funcName = idc.get_name(idc.get_operand_value(address, 0), idc.ida_name.GN_VISIBLE)
        return funcName
    
    # we don't know the number of args to a given function and we're not considering SSE args
    # this is just a convenience, use the emulator object if you have specific needs
    def getArgv(self):
        if self.arch == unicorn.UC_ARCH_X86:
            if self.mode == unicorn.UC_MODE_64:
                if self.filetype == "MACHO" or self.filetype == "ELF":
                    argv = [
                        self.getRegVal("rdi"),
                        self.getRegVal("rsi"),
                        self.getRegVal("rdx"),
                        self.getRegVal("rcx"),
                        self.getRegVal("r8"),
                        self.getRegVal("r9")]
                else:
                    argv = [
                        self.getRegVal("rcx"),
                        self.getRegVal("rdx"),
                        self.getRegVal("r8"),
                        self.getRegVal("r9")]
            else:
                sp = self.getRegVal("esp")
                argv = [
                    struct.unpack("<I", str(self.uc.mem_read(sp, 4)))[0],
                    struct.unpack("<I", str(self.uc.mem_read(sp + 4, 4)))[0],
                    struct.unpack("<I", str(self.uc.mem_read(sp + 8, 4)))[0],
                    struct.unpack("<I", str(self.uc.mem_read(sp + 12, 4)))[0],
                    struct.unpack("<I", str(self.uc.mem_read(sp + 16, 4)))[0],
                    struct.unpack("<I", str(self.uc.mem_read(sp + 20, 4)))[0]]
        elif self.arch == unicorn.UC_ARCH_ARM:
            argv = [
                self.getRegVal("R0"),
                self.getRegVal("R1"),
                self.getRegVal("R2"),
                self.getRegVal("R3")]
        elif self.arch == unicorn.UC_ARCH_ARM64:
            argv = [
                self.getRegVal("X0"),
                self.getRegVal("X1"),
                self.getRegVal("X2"),
                self.getRegVal("X3"),
                self.getRegVal("X4"),
                self.getRegVal("X5"),
                self.getRegVal("X6"),
                self.getRegVal("X7")]
        else:
            argv = None
                
        return argv
 
    def _checkMemSize(self, size, userData):
        if size > MAX_ALLOC_SIZE:
            logging.debug("allocation size (%s) truncated @%s" % 
                          (self.hexString(size), self.hexString(userData["currAddr"])))
            size = MAX_ALLOC_SIZE
        return size
            
    ############################################
    # BEGIN API HOOKS
    ############################################
    
    # return a fake handle value
    def _returnHandleHook(self, address, argv, funcName, userData):
        self.uc.reg_write(self.regs["ret"], 42)
        
    def _returnParam1Hook(self, address, argv, funcName, userData):
        self.uc.reg_write(self.regs["ret"], argv[0])
    
    def _allocMem1Hook(self, address, argv, funcName, userData):
        allocSize = argv[0]
        allocSize = self._checkMemSize(allocSize, userData)
        self.uc.reg_write(self.regs["ret"], self.allocEmuMem(allocSize))
        
    def _allocMem2Hook(self, address, argv, funcName, userData):
        allocSize = argv[1]
        allocSize = self._checkMemSize(allocSize, userData)
        self.uc.reg_write(self.regs["ret"], self.allocEmuMem(allocSize))
        
    def _allocMem3Hook(self, address, argv, funcName, userData):
        allocSize = argv[2]
        allocSize = self._checkMemSize(allocSize, userData)
        self.uc.reg_write(self.regs["ret"], self.allocEmuMem(allocSize))
        
    def _callocHook(self, address, argv, funcName, userData):
        allocSize = argv[0] * argv[1]
        allocSize = self._checkMemSize(allocSize, userData)
        self.uc.reg_write(self.regs["ret"], self.allocEmuMem(allocSize))
        
    # deny "in place only" flag
    def _heapReAllocHook(self, address, argv, funcName, userData):
        HEAP_REALLOC_IN_PLACE_ONLY = 0x10
        if argv[1] & HEAP_REALLOC_IN_PLACE_ONLY:
            self.uc.reg_write(self.regs["ret"], 0)
        else:
            allocSize = argv[3]
            allocSize = self._checkMemSize(allocSize, userData)
            region = self.getEmuMemRegion(argv[2])
            if region is not None:
                allocSize = max(region[1] - region[0], allocSize)
                memAddr = self.allocEmuMem(allocSize)
                self.copyEmuMem(memAddr, region[0], region[1] - region[0], userData)
            else:
                memAddr = self.allocEmuMem(allocSize)
            self.uc.reg_write(self.regs["ret"], memAddr)
        
            
    def _reallocHook(self, address, argv, funcName, userData):
        allocSize = argv[1]
        allocSize = self._checkMemSize(allocSize, userData)
        region = self.getEmuMemRegion(argv[0])
        if region is not None:
            allocSize = max(region[1] - region[0], allocSize)
            memAddr = self.allocEmuMem(allocSize)
            self.copyEmuMem(memAddr, region[0], region[1] - region[0], userData)
        else:
            memAddr = self.allocEmuMem(allocSize)
        self.uc.reg_write(self.regs["ret"], memAddr)
            
    # allocate regardless of commit flag, keep a mapping of requested addr -> actual addr
    def _virtualAllocHook(self, address, argv, funcName, userData):
        allocAddr = argv[0]
        if allocAddr in self.allocMap:
            self.uc.reg_write(self.regs["ret"], self.allocMap[allocAddr][0])
            return
        allocSize = argv[1]
        allocSize = self._checkMemSize(allocSize, userData)  
        memAddr = self.allocEmuMem(allocSize, allocAddr)
        self.allocMap[allocAddr] = (memAddr, allocSize)
        self.uc.reg_write(self.regs["ret"], memAddr)
        
    # handle same as VirtualAlloc hook, just with different argument placement
    def _virtualAllocExHook(self, address, argv, funcName, userData):
        allocAddr = argv[1]
        if allocAddr in self.allocMap:
            self.uc.reg_write(self.regs["ret"], self.allocMap[allocAddr][0])
            return
        allocSize = argv[2]
        allocSize = self._checkMemSize(allocSize, userData)  
        memAddr = self.allocEmuMem(allocSize, allocAddr)
        self.allocMap[allocAddr] = (memAddr, allocSize)
        self.uc.reg_write(self.regs["ret"], memAddr)
        
    def _memcpyHook(self, address, argv, funcName, userData):
        copySize = argv[2]
        copySize = self._checkMemSize(copySize, userData)
        srcRegion = self.getEmuMemRegion(argv[1])
        dstRegion = self.getEmuMemRegion(argv[0])
        if dstRegion is None:
            logging.debug("dest memory does not exist for memcpy @%s" % self.hexString(address))
            dstRegion = self.getEmuMemRegion(self.allocEmuMem(copySize))
            argv[0] = dstRegion[0]
        if srcRegion is None:
            logging.debug("source memory does not exist for memcpy @%s" % self.hexString(address))
        else:
            if copySize <= srcRegion[1] - argv[1] and copySize <= dstRegion[1] - argv[0]:
                self.copyEmuMem(argv[0], argv[1], copySize, userData)
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))
        self.uc.reg_write(self.regs["ret"], argv[0])
        
    def _strlenHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            self.uc.reg_write(self.regs["ret"], len(self.getEmuString(argv[0])))
        else:
            self.uc.reg_write(self.regs["ret"], 0)
            
    def _wcslenHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            self.uc.reg_write(self.regs["ret"], len(self.getEmuWideString(argv[0]).decode("utf-16")))
        else:
            self.uc.reg_write(self.regs["ret"], 0)
    
    def _strnlenHook(self, address, argv, funcName, userData):
        strnlen = self._checkMemSize(argv[1], userData)
        if self.isValidEmuPtr(argv[0]):
            strlen = len(self.getEmuString(argv[0]))
            strlen = min(strlen, strnlen)
            self.uc.reg_write(self.regs["ret"], strlen)
        else:
            self.uc.reg_write(self.regs["ret"], 0)
            
    def _wcsnlenHook(self, address, argv, funcName, userData):
        strnlen = self._checkMemSize(argv[1], userData)
        if self.isValidEmuPtr(argv[0]):
            strlen = len(self.getEmuWideString(argv[0]).decode("utf-16"))
            if strlen > strnlen:
                strlen = argv[1]
            self.uc.reg_write(self.regs["ret"], strnlen)
        else:
            self.uc.reg_write(self.regs["ret"], 0)
    
    def _strcmpHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]) and self.isValidEmuPtr(argv[1]):
            str1 = self.getEmuString(argv[1])
            str2 = self.getEmuString(argv[0])
            if str1 == str2:
                self.uc.reg_write(self.regs["ret"], 0)
                return
        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _strncmpHook(self, address, argv, funcName, userData):
        strnlen = self._checkMemSize(argv[2], userData)
        if self.isValidEmuPtr(argv[0]) and self.isValidEmuPtr(argv[1]):
            str1 = self.getEmuString(argv[1])
            str2 = self.getEmuString(argv[0])
            if str1[:strnlen] == str2[:strnlen]:
                self.uc.reg_write(self.regs["ret"], 0)
                return
        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _stricmpHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]) and self.isValidEmuPtr(argv[1]):
            str1 = self.getEmuString(argv[1])
            str2 = self.getEmuString(argv[0])
            if str1.lower() == str2.lower():
                self.uc.reg_write(self.regs["ret"], 0)
                return
        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _strnicmpHook(self, address, argv, funcName, userData):
        strnlen = self._checkMemSize(argv[2], userData)
        if self.isValidEmuPtr(argv[0]) and self.isValidEmuPtr(argv[1]):
            str1 = self.getEmuString(argv[1])
            str2 = self.getEmuString(argv[0])
            if str1[:strnlen].lower() == str2[:strnlen].lower():
                self.uc.reg_write(self.regs["ret"], 0)
                return
        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _wcscmpHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]) and self.isValidEmuPtr(argv[1]):
            str1 = self.getEmuWideString(argv[1]).decode("utf-16")
            str2 = self.getEmuWideString(argv[0]).decode("utf-16")
            if str1 == str2:
                self.uc.reg_write(self.regs["ret"], 0)
                return
        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _wcsncmpHook(self, address, argv, funcName, userData):
        strnlen = self._checkMemSize(argv[2], userData)
        if self.isValidEmuPtr(argv[0]) and self.isValidEmuPtr(argv[1]):
            str1 = self.getEmuWideString(argv[1]).decode("utf-16")
            str2 = self.getEmuWideString(argv[0]).decode("utf-16")
            if str1[:strnlen] == str2[:strnlen]:
                self.uc.reg_write(self.regs["ret"], 0)
                return
        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _wcsicmpHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]) and self.isValidEmuPtr(argv[1]):
            str1 = self.getEmuWideString(argv[1]).decode("utf-16")
            str2 = self.getEmuWideString(argv[0]).decode("utf-16")
            if str1.lower() == str2.lower():
                self.uc.reg_write(self.regs["ret"], 0)
                return
        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _wcsnicmpHook(self, address, argv, funcName, userData):
        strnlen = self._checkMemSize(argv[2], userData)
        if self.isValidEmuPtr(argv[0]) and self.isValidEmuPtr(argv[1]):
            str1 = self.getEmuWideString(argv[1]).decode("utf-16")
            str2 = self.getEmuWideString(argv[0]).decode("utf-16")
            if str1[:strnlen].lower() == str2[:strnlen].lower():
                self.uc.reg_write(self.regs["ret"], 0)
                return
        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _strcpyHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            src = self.getEmuString(argv[1]) + "\x00"
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for strcpy @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(len(src)))
                argv[0] = dstRegion[0]
            if len(src) <= dstRegion[1] - argv[0]:
                self.uc.mem_write(argv[0], src)
                self.uc.reg_write(self.regs["ret"], argv[0])
                return
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))

        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _strncpyHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            strnlen = self._checkMemSize(argv[2], userData)
            src = self.getEmuString(argv[1])
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for strncpy @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(strnlen))
                argv[0] = dstRegion[0]
            if strnlen <= dstRegion[1] - argv[0]:
                if strnlen > len(src):
                    src = src.ljust(strnlen, "\x00")
                self.uc.mem_write(argv[0], src)
                self.uc.reg_write(self.regs["ret"], argv[0])
                return
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))

        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _strncpysHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[2]):
            strnlen = self._checkMemSize(argv[3], userData)
            src = self.getEmuString(argv[2])
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for strncpy_s @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(strnlen))
                argv[0] = dstRegion[0]
            
            strnlen = min(strnlen, len(src))
            if strnlen + 1 <= dstRegion[1] - argv[0]:
                self.uc.mem_write(argv[0], src + "\x00")
                self.uc.reg_write(self.regs["ret"], 0)
                return
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))

        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
       
    def _wcscpyHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            src = self.getEmuWideString(argv[1]) + "\x00\x00"
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for wcscpy @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(len(src)))
                argv[0] = dstRegion[0]
            if len(src) <= dstRegion[1] - argv[0]:
                self.uc.mem_write(argv[0], src)
                self.uc.reg_write(self.regs["ret"], argv[0])
                return
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))

        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _wcsncpyHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            strnlen = self._checkMemSize(argv[2] * 2, userData)
            src = self.getEmuWideString(argv[1])
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for wcsncpy @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(strnlen))
                argv[0] = dstRegion[0]
            if strnlen <= dstRegion[1] - argv[0]:
                if strnlen > len(src):
                    src = src.ljust(strnlen, "\x00")
                self.uc.mem_write(argv[0], src)
                self.uc.reg_write(self.regs["ret"], argv[0])
                return
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))

        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
        
    def _wcsncpysHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[2]):
            strnlen = self._checkMemSize(argv[3] * 2, userData)
            src = self.getEmuWideString(argv[2])
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for wcsncpy_s @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(strnlen))
                argv[0] = dstRegion[0]
                
            strnlen = min(strnlen, len(src))
            if strnlen + 2 <= dstRegion[1] - argv[0]:
                self.uc.mem_write(argv[0], src[:strnlen] + "\x00\x00")
                self.uc.reg_write(self.regs["ret"], 0)
                return
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))

        if self.size_pointer == 8:
            val = 0xffffffffffffffff
        else:
            val = 0xffffffff
        self.uc.reg_write(self.regs["ret"], val)
    
    def _memchrHook(self, address, argv, funcName, userData):
        dstRegion = self.getEmuMemRegion(argv[0])
        if dstRegion is not None:
            srch = chr(argv[1] & 0xFF)
            srchlen = argv[2]
            # truncate search to end of region
            if argv[0] + srchlen > dstRegion[1]:
                srchlen = dstRegion[1] - argv[0]
            buf = str(self.uc.mem_read(argv[0], srchlen))
            offs = buf.find(srch)
            if offs > -1:
                self.uc.reg_write(self.regs["ret"], argv[0] + offs)
                return
                
        self.uc.reg_write(self.regs["ret"], 0)
        
    def _mbstowcsHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            bufSize = self._checkMemSize(argv[2] * 2, userData)
            src = self.getEmuString(argv[1])
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for mbtowc variant @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(bufSize))
                argv[0] = dstRegion[0]

            if argv[2] > len(src):
                src = src.ljust(argv[2], "\x00")
            else:
                src += "\x00"
            if len(src.encode("utf-16")[2:]) <= dstRegion[1] - argv[0]:
                self.uc.mem_write(argv[0], src.encode("utf-16")[2:])
                self.uc.reg_write(self.regs["ret"], len(src))
                return
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))

        self.uc.reg_write(self.regs["ret"], 0)
        
    def _mbtowcHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            src = self.getEmuString(argv[1])[0]
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for mbtowc variant @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(0x1000))
                argv[0] = dstRegion[0]
            self.uc.mem_write(argv[0], src.encode("utf-16")[2:4])
            self.uc.reg_write(self.regs["ret"], 1)
            return

        self.uc.reg_write(self.regs["ret"], 0)
        
    def _mbstowcsHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            maxBufSize = self._checkMemSize(argv[2] * 2, userData)
            src = self.getEmuString(argv[1])
            if len(src) < argv[2]:
                src += "\x00"
            else:
                src = src[:argv[2]]
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for mbtowc variant @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(maxBufSize))
                argv[0] = dstRegion[0]
            if len(src) * 2 + 2 <= dstRegion[1] - argv[0]:
                self.uc.mem_write(argv[0], src.encode("utf-16")[2:] + "\x00\x00")
                self.uc.reg_write(self.regs["ret"], len(src.replace("\x00", "")))
                return
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))

        self.uc.reg_write(self.regs["ret"], 0)
        
    def _wctombHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            src = self.getEmuWideString(argv[1]).decode("utf-16")
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for wctomb variant @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(0x1000))
            argv[0] = dstRegion[0]
            self.uc.mem_write(argv[0], src[0])
            self.uc.reg_write(self.regs["ret"], 1)
            return

        self.uc.reg_write(self.regs["ret"], 0)
        
    def _wcstombsHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            bufSize = self._checkMemSize(argv[2], userData)
            src = self.getEmuWideString(argv[1]).decode("utf-16")
            if len(src) < argv[2]:
                src += "\x00"
            else:
                src = src[:argv[2]]
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for wctomb variant @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(bufSize))
                argv[0] = dstRegion[0]
            if bufSize + 1 <= dstRegion[1] - argv[0]:
                if bufSize > len(src):
                    src = src.ljust(bufSize, "\x00")
                self.uc.mem_write(argv[0], src + "\x00")
                self.uc.reg_write(self.regs["ret"], len(src.replace("\x00", "")))
                return
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))

        self.uc.reg_write(self.regs["ret"], 0)
        
    def _multiByteToWideCharHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[2]):
            src = self.getEmuString(argv[2])
            if argv[3] == -1:
                src += "\x00"
                maxBufSize = self._checkMemSize(len(src) * 2, userData)
            else:
                maxBufSize = self._checkMemSize(argv[3] * 2, userData)
            
            if len(src) < argv[3]:
                src += "\x00"
            elif argv[3] != -1:
                src = src[:argv[3]]
                
            if argv[5] == 0:
                self.uc.reg_write(self.regs["ret"], len(src) * 2)
                return
            dstRegion = self.getEmuMemRegion(argv[4])
            if dstRegion is None:
                logging.debug("dest memory does not exist for mbtowc variant @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(maxBufSize))
                argv[4] = dstRegion[0]
            if len(src) * 2 + 2 <= dstRegion[1] - argv[4]:
                self.uc.mem_write(argv[4], src.encode("utf-16")[2:] + "\x00\x00")
                self.uc.reg_write(self.regs["ret"], len(src))
                return
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))

        self.uc.reg_write(self.regs["ret"], 0)
        
    def _wideCharToMultiByteHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[2]):
            src = self.getEmuWideString(argv[2]).decode("utf-16")
            if argv[3] == -1:
                src += "\x00"
                maxBufSize = self._checkMemSize(len(src), userData)
            else:
                maxBufSize = self._checkMemSize(argv[3], userData)
            
            if len(src) < argv[3]:
                src += "\x00"
            elif argv[3] != -1:
                src = src[:argv[3]]
                
            if argv[5] == 0:
                self.uc.reg_write(self.regs["ret"], len(src))
                return
            dstRegion = self.getEmuMemRegion(argv[4])
            if dstRegion is None:
                logging.debug("dest memory does not exist for mbtowc variant @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(maxBufSize))
                argv[4] = dstRegion[0]
            if len(src) + 1 <= dstRegion[1] - argv[4]:
                self.uc.mem_write(argv[4], src + "\x00")
                self.uc.reg_write(self.regs["ret"], len(src))
                return
            else:
                logging.debug("dest memory not large enough @%s" % self.hexString(address))

        self.uc.reg_write(self.regs["ret"], 0)
        
    def _memsetHook(self, address, argv, funcName, userData):
        setSize = argv[2]
        setSize = self._checkMemSize(setSize, userData)
        dstRegion = self.getEmuMemRegion(argv[0])
        src = chr(argv[1] & 0xFF)
        if dstRegion is None:
            logging.debug("dest memory does not exist for memset @%s" % self.hexString(address))
            dstRegion = self.getEmuMemRegion(self.allocEmuMem(setSize))
            argv[0] = dstRegion[0]
        if setSize <= dstRegion[1] - argv[0]:
            self.uc.mem_write(argv[0], src * setSize)
        else:
            logging.debug("dest memory not large enough @%s" % self.hexString(address))
        self.uc.reg_write(self.regs["ret"], argv[0])
    
    def _bzeroHook(self, address, argv, funcName, userData):
        setSize = argv[1]
        setSize = self._checkMemSize(setSize, userData)
        dstRegion = self.getEmuMemRegion(argv[0])
        src = "\x00"
        if dstRegion is None:
            logging.debug("dest memory does not exist for memset @%s" % self.hexString(address))
            dstRegion = self.getEmuMemRegion(self.allocEmuMem(setSize))
            argv[0] = dstRegion[0]
        if setSize <= dstRegion[1] - argv[0]:
            self.uc.mem_write(argv[0], src * setSize)
        else:
            logging.debug("dest memory not large enough @%s" % self.hexString(address))
        self.uc.reg_write(self.regs["ret"], argv[0])
        
    def _strcatHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            src = self.getEmuString(argv[1]) + "\x00"
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for strcat @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(len(src) + 1))
                argv[0] = dstRegion[0]
            dst = self.getEmuString(argv[0])
            if len(dst) + len(src) <= dstRegion[1] - argv[0]:
                self.uc.mem_write(argv[0], dst + src)
                self.uc.reg_write(self.regs["ret"], argv[0])
                return

        self.uc.reg_write(self.regs["ret"], 0)
                    
    def _strncatHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            strnlen = self._checkMemSize(argv[2], userData)
            src = self.getEmuString(argv[1])
            strnlen = min(strnlen, len(src))
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for strncat @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(strnlen + 1))
                argv[0] = dstRegion[0]
            dst = self.getEmuString(argv[0])
            if len(dst) + strnlen + 1 <= dstRegion[1] - argv[0]:
                self.uc.mem_write(argv[0], dst + src[:strnlen] + "\x00")
                self.uc.reg_write(self.regs["ret"], argv[0])
                return

        self.uc.reg_write(self.regs["ret"], 0)
    
    def _wcscatHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            src = self.getEmuWideString(argv[1]) + "\x00\x00"
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for wcscat @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(len(src)))
                argv[0] = dstRegion[0]
            dst = self.getEmuWideString(argv[0])
            if len(dst) + len(src) <= dstRegion[1] - argv[0]:
                self.uc.mem_write(argv[0], dst + src)
                self.uc.reg_write(self.regs["ret"], argv[0])
                return

        self.uc.reg_write(self.regs["ret"], 0)
    
    def _wcsncatHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[1]):
            strnlen = self._checkMemSize(argv[2], userData)
            src = self.getEmuWideString(argv[1])
            strnlen = min(strnlen * 2, len(src))
            dstRegion = self.getEmuMemRegion(argv[0])
            if dstRegion is None:
                logging.debug("dest memory does not exist for wcsncat @%s" % self.hexString(address))
                dstRegion = self.getEmuMemRegion(self.allocEmuMem(strnlen + 2))
                argv[0] = dstRegion[0]
            dst = self.getEmuWideString(argv[0])
            if len(dst) + strnlen + 2 <= dstRegion[1] - argv[0]:
                self.uc.mem_write(argv[0], dst + src[:strnlen] + "\x00\x00")
                self.uc.reg_write(self.regs["ret"], argv[0])
                return

        self.uc.reg_write(self.regs["ret"], 0)
        
    def _strchrHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            s = self.getEmuString(argv[0])
            idx = s.find(chr(argv[1] & 0xFF))
            if idx != -1:
                self.uc.reg_write(self.regs["ret"], argv[0] + idx)
                return

        self.uc.reg_write(self.regs["ret"], 0)
    
    def _wcschrHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            s = self.getEmuWideString(argv[0]).decode("utf-16")
            idx = s.find(chr(argv[1] & 0xFF))
            if idx != -1:
                self.uc.reg_write(self.regs["ret"], argv[0] + idx * 2)
                return

        self.uc.reg_write(self.regs["ret"], 0)
    
    def _strrchrHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            s = self.getEmuString(argv[0])
            idx = s.rfind(chr(argv[1] & 0xFF))
            if idx != -1:
                self.uc.reg_write(self.regs["ret"], argv[0] + idx)
                return
        
        self.uc.reg_write(self.regs["ret"], 0)
            
    def _wcsrchrHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            s = self.getEmuWideString(argv[0]).decode("utf-16")
            idx = s.rfind(chr(argv[1] & 0xFF))
            if idx != -1:
                self.uc.reg_write(self.regs["ret"], argv[0] + idx * 2)
                return

        self.uc.reg_write(self.regs["ret"], 0)
        
    def _strlwrHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            s = self.getEmuString(argv[0])
            self.uc.mem_write(argv[0], s.lower())
            self.uc.reg_write(self.regs["ret"], argv[0])
            return
        
        self.uc.reg_write(self.regs["ret"], 0)
        
    def _struprHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            s = self.getEmuString(argv[0])
            self.uc.mem_write(argv[0], s.upper())
            self.uc.reg_write(self.regs["ret"], argv[0])
            return
        
        self.uc.reg_write(self.regs["ret"], 0)
        
    def _wcslwrHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            s = self.getEmuWideString(argv[0]).decode("utf-16")
            self.uc.mem_write(argv[0], s.lower().encode("utf-16")[2:])
            self.uc.reg_write(self.regs["ret"], argv[0])
            return
        
        self.uc.reg_write(self.regs["ret"], 0)
        
    def _wcsuprHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            s = self.getEmuWideString(argv[0]).decode("utf-16")
            self.uc.mem_write(argv[0], s.upper().encode("utf-16")[2:])
            self.uc.reg_write(self.regs["ret"], argv[0])
            return
        
        self.uc.reg_write(self.regs["ret"], 0)
        
    def _strdupHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            s = self.getEmuString(argv[0])
            memAddr = self.allocEmuMem(len(s) + 1)
            self.uc.mem_write(memAddr, s)
            self.uc.reg_write(self.regs["ret"], memAddr)
            return
        
        self.uc.reg_write(self.regs["ret"], 0)
        
    def _wcsdupHook(self, address, argv, funcName, userData):
        if self.isValidEmuPtr(argv[0]):
            s = self.getEmuWideString(argv[0])
            memAddr = self.allocEmuMem(len(s) + 2)
            self.uc.mem_write(memAddr, s)
            self.uc.reg_write(self.regs["ret"], memAddr)
            return
        
        self.uc.reg_write(self.regs["ret"], 0)
        
    def _modHook(self, address, argv, funcName, userData):
        self.uc.reg_write(self.regs["ret"], argv[0] % argv[1])
        
    ############################################
    # END API HOOKS
    ############################################
        
    # maps null memory as requested during emulation
    def _hookMemInvalid(self, uc, access, address, size, value, userData):
        logging.debug("invalid memory operation for %s @%s" %
                      (self.hexString(address), self.hexString(userData['currAddr'])))
        try:
            uc.mem_map(address & self.pageMask, PAGESIZE)
            uc.mem_write(address & self.pageMask, "\x00" * PAGESIZE)
        except Exception:
            logging.debug("error writing to %s, changing IP from %s to %s" % (self.hexString(address), self.hexString(
                userData['currAddr']), self.hexString(userData['currAddr'] + userData['currAddrSize'])))
            uc.reg_write(
                self.regs["pc"], userData['currAddr'] + userData['currAddrSize'])
        return True

    # cannot seem to move IP forward from this hook for some reason..
    # patches current instruction with NOPs
    def _hookInterrupt(self, uc, intno, userData):
        logging.debug("interrupt #%d received @%s" % ((intno), self.hexString(userData["currAddr"])))
        if self.arch == unicorn.UC_ARCH_X86:
            uc.mem_write(userData["currAddr"], X86NOP *
                         userData["currAddrSize"])
        elif self.arch == unicorn.UC_ARCH_ARM:
            if self.mode == unicorn.UC_MODE_THUMB:
                uc.mem_write(userData["currAddr"],
                             ARMTHUMBNOP * (userData["currAddrSize"] / 2))
            else:
                uc.mem_write(
                    userData["currAddr"], ARMNOP * (userData["currAddrSize"] / 4))
        elif self.arch == unicorn.UC_ARCH_ARM64:
            uc.mem_write(
                userData["currAddr"], ARM64NOP * (userData["currAddrSize"] / 4))
        self.enteredBlock = False
        return True

    # handle common runtime functions
    def _handleApiHooks(self, address, argv, funcName, userData):
        # normalize funcName
        # remove appended _n from IDA Pro names
        funcName = re.sub(r"_[\d]+$", "", funcName)
            
        # remove appended _l for locale flavors of string functions
        funcName = re.sub(r"_l$", "", funcName)
        
        # remove IDA Pro's j_ prefix
        if funcName[:2] == "j_":
            funcName = funcName[2:]

        # remove prepended underscores
        funcName = re.sub(r"^_+", "", funcName)
        
        if funcName not in self.apiHooks:
            return False
        try:
            self.apiHooks[funcName](address, argv, funcName, userData)
        except Exception as e:
            logging.debug("error handling API hook: %s @%s" % (e, self.hexString(address)))
            
        self.skipInstruction(userData)
        return True
    
    # instruction hook used by emulateRange function
    # implements bare bones instrumentation to handle basic code flow
    def _emulateRangeCodeHook(self, uc, address, size, userData):
        try:
            userData['currAddr'] = address
            userData['currAddrSize'] = size
            if self.arch == unicorn.UC_ARCH_ARM and userData["changeThumbMode"]:
                self._handleThumbMode(address)
                userData["changeThumbMode"] = False
                return

            if self.verbose > 0:
                if self.verbose > 1:
                    logging.debug(self.getEmuState())
                dis = idc.generate_disasm_line(address, 0)
                logging.debug("%s: %s" % (self.hexString(address), dis))

            # stop emulation if specified endAddr is reached
            if userData["endAddr"] is not None:
                if address == userData["endAddr"]:
                    self.stopEmulation(userData)
                    return
            if self._isBadBranch(userData):
                self.skipInstruction(userData)
                return
            # stop annoying run ons if we end up somewhere we dont belong
            if str(self.uc.mem_read(address, size)) == "\x00" * size:
                logging.debug("pc ended up in null memory @%s" %
                              self.hexString(address))
                self.stopEmulation(userData)
                return

            # otherwise, stop emulation when returning from function emulation began in
            elif (self.isRetInstruction(address) and
                    idc.get_func_attr(address, idc.FUNCATTR_START) ==
                    userData["funcStart"]):
                self.stopEmulation(userData)
                return
            elif self.isRetInstruction(address) and self.arch == unicorn.UC_ARCH_ARM:
                # check mode of return address if ARM
                retAddr = self.getEmuPtr(self.getRegVal("LR"))
                if self.isThumbMode(retAddr):
                    userData["changeThumbMode"] = True

            if (idc.print_insn_mnem(address) in self.callMnems or
                    (idc.print_insn_mnem(address) == "B" and
                     idc.get_name_ea_simple(idc.print_operand(address, 0)) ==
                     idc.get_func_attr(
                        idc.get_name_ea_simple(idc.print_operand(address, 0)),
                        idc.FUNCATTR_START))):
                        
                funcName = self.getCallTargetName(address)
                if userData["callHook"]:
                    userData["callHook"](address, self.getArgv(), funcName, userData)

                if self.arch == unicorn.UC_ARCH_ARM:
                    userData["changeThumbMode"] = True
                    
                # if the pc has been changed by the hook, don't skip instruction and undo the change
                if self.getRegVal("pc") != userData["currAddr"]:
                    # get IDA's SP delta value for next instruction to adjust stack accordingly since we are skipping this
                    # instruction
                    uc.reg_write(self.regs["sp"], self.getRegVal("sp") +
                                 idaapi.get_sp_delta(userData["func_t"], self.getRegVal("pc")))
                    return
                 
                if userData["hookApis"] and self._handleApiHooks(address, self.getArgv(), funcName, userData):
                    return
                
                # skip calls if specified or there are no instructions to emulate at destination address
                if (userData["skipCalls"] is True or
                        (idc.get_operand_type(address, 0) == 7 and
                         str(uc.mem_read(idc.get_operand_value(address, 0), self.size_pointer)) ==
                         "\x00" * self.size_pointer)):
                    self.skipInstruction(userData)
            # handle x86 instructions moving import pointers to a register
            elif (idc.print_insn_mnem(address) == "mov" and 
                  idc.get_operand_type(address, 1) == 2 and 
                  idc.get_operand_type(address, 0) == 1 and
                  idc.print_operand(address, 1)[:3] == "ds:" and 
                  str(uc.mem_read(idc.get_operand_value(address, 1), self.size_pointer)) ==
                  "\x00" * self.size_pointer):
                  uc.reg_write(self.regs[idc.print_operand(address, 0)], idc.get_operand_value(address, 1))
                  self.skipInstruction(userData)

        except Exception as err:
            logging.debug("exception in emulateRange_codehook @%s: %s" % (self.hexString(address), str(err)))
            print("exception in emulateRange_codehook @%s: %s" % (self.hexString(address), str(err)))
            self.stopEmulation(userData)

    # instruction hook used by emulateBytes function
    # implements bare bones instrumentation to handle basic code flow
    def _emulateBytesCodeHook(self, uc, address, size, userData):
        try:
            userData['currAddr'] = address
            userData['currAddrSize'] = size
            # stop emulation if specified endAddr is reached
            if userData["endAddr"] is not None:
                if address == userData["endAddr"]:
                    self.stopEmulation(userData)
                    return

            # stop annoying run ons if we end up somewhere we dont belong
            if str(self.uc.mem_read(address, 0x10)) == "\x00" * 0x10:
                self.stopEmulation(userData)
                logging.debug("pc ended up in null memory @%s" %
                              self.hexString(address))
                return

        except Exception as err:
            logging.debug("exception in emulateBytes_codehook @%s: %s" % (self.hexString(address), str(err)))
            print("exception in emulateBytes_codehook @%s: %s" % (self.hexString(address), str(err)))
            self.stopEmulation(userData)

    # this instruction hook is used by the iterate feature, forces execution down a specified path
    def _guidedHook(self, uc, address, size, userData):
        try:
            userData['currAddr'] = address
            userData['currAddrSize'] = size
            if self.arch == unicorn.UC_ARCH_ARM and userData["changeThumbMode"]:
                self._handleThumbMode(address)
                userData["changeThumbMode"] = False
                return
            if self.verbose > 0:
                if self.verbose > 1:
                    logging.debug(self.getEmuState())
                dis = idc.generate_disasm_line(address, 0)
                logging.debug("%s: %s" % (self.hexString(address), dis))
            if self.arch == unicorn.UC_ARCH_ARM:
                # since there are lots of bad branches during emulation and we are forcing it anyways
                if idc.print_insn_mnem(address)[:3] in ["TBB", "TBH"]:
                    # skip over interleaved jump table
                    nextInsnAddr = self._scanForCode(address + size)
                    self.changeProgramCounter(userData, nextInsnAddr)
                    return
            elif self._isBadBranch(userData):
                self.skipInstruction(userData)
                return

            flow, paths = userData["targetInfo"][userData["targetVA"]]
            # check if we are out of our block bounds or re-entering our block in a loop
            bbEnd = idc.prev_head(
                flow[paths[self.pathIdx][self.blockIdx]][1], idc.get_inf_attr(idc.INF_MIN_EA))
            bbStart = flow[paths[self.pathIdx][self.blockIdx]][0]
            if address == bbStart and self.enteredBlock is True:
                if self.blockIdx < len(paths[self.pathIdx]) - 1:
                    logging.debug("loop re-entering block #%d (%s -> %s), forcing PC to %s" %
                                  (self.blockIdx, self.hexString(bbStart), self.hexString(bbEnd),
                                   self.hexString(flow[paths[self.pathIdx][self.blockIdx + 1]][0])))
                    # force PC to follow paths
                    uc.reg_write(self.regs["pc"], flow[paths[self.pathIdx][self.blockIdx + 1]][0])
                    self.blockIdx += 1
                    self.enteredBlock = False
                    if self.arch == unicorn.UC_ARCH_ARM:
                        userData["changeThumbMode"] = True
                    return
                else:
                    logging.debug(
                        "loop re-entering block #%d (%s -> %s), but no more blocks! bailing out of this function.." %
                        (self.blockIdx, self.hexString(bbStart), self.hexString(bbEnd)))
                    self.stopEmulation(userData)
                    return
            elif (address > bbEnd or address < bbStart):
                # check if we skipped over our target (our next block index is out of range), this can happen in ARM
                # with conditional instructions
                if self.blockIdx + 1 >= len(paths[self.pathIdx]):
                    logging.debug(
                        "we missed our target! bailing out of this function..")
                    self.stopEmulation(userData)
                    return
                logging.debug("%s is outside of block #%d (%s -> %s), forcing PC to %s" %
                              (self.hexString(address),
                               self.blockIdx, self.hexString(bbStart),
                               self.hexString(bbEnd), self.hexString(flow[paths[self.pathIdx][self.blockIdx + 1]][0])))
                # force PC to follow paths
                uc.reg_write(self.regs["pc"], flow[paths[self.pathIdx][self.blockIdx + 1]][0])
                self.blockIdx += 1
                self.enteredBlock = False
                if self.arch == unicorn.UC_ARCH_ARM:
                    userData["changeThumbMode"] = True
                return

            if address == bbStart:
                self.enteredBlock = True
            # possibly a folded instruction or invalid instruction
            if idc.print_insn_mnem(address) == "":
                if idc.print_insn_mnem(address + size) == "":
                    if idc.print_insn_mnem(address + size * 2) == "":
                        logging.debug(
                            "invalid instruction encountered @%s, bailing.." % self.hexString(address))
                        self.stopEmulation(userData)
                    return
                return

            # stop annoying run ons if we end up somewhere we dont belong
            if str(self.uc.mem_read(address, 0x10)) == "\x00" * 0x10:
                logging.debug("pc ended up in null memory @%s" %
                              self.hexString(address))
                self.stopEmulation(userData)
                return

            # this is our stop, this is where we trigger user-defined callback with our info
            if address == userData["targetVA"]:
                logging.debug("target %s hit" %
                              self.hexString(userData["targetVA"]))
                self._targetHit(address, userData)
                self.stopEmulation(userData)
            elif address in userData["targetInfo"]:
                # this address is another target in the dict, process it and continue onward
                logging.debug("target %s found on the way to %s" % (
                    self.hexString(address), self.hexString(userData["targetVA"])))
                self._targetHit(address, userData)

            if (idc.print_insn_mnem(address) in self.callMnems or
                (idc.print_insn_mnem(address) == "B" and
                 idc.get_name_ea_simple(idc.print_operand(address, 0)) ==
                 idc.get_func_attr(
                 idc.get_name_ea_simple(idc.print_operand(address, 0)),
                 idc.FUNCATTR_START))):
                 
                funcName = self.getCallTargetName(address)
                if userData["callHook"]:
                    userData["callHook"](address, self.getArgv(), funcName, userData)

                
                if self.arch == unicorn.UC_ARCH_ARM:
                    userData["changeThumbMode"] = True

                # if the pc has been changed by the hook, don't skip instruction and undo the change
                if self.getRegVal("pc") != userData["currAddr"]:
                    # get IDA's SP delta value for next instruction to adjust stack accordingly since we are skipping this
                    # instruction
                    uc.reg_write(self.regs["sp"], self.getRegVal("sp") +
                                 idaapi.get_sp_delta(userData["func_t"], self.getRegVal("pc")))
                    return
                
                if userData["hookApis"] and self._handleApiHooks(address, self.getArgv(), funcName, userData):
                    return
                    
                # if you change the program counter, it undoes your call to emu_stop()
                if address != userData["targetVA"]:
                    self.skipInstruction(userData)
                    
            elif self.isRetInstruction(address):
                # self.stopEmulation(userData)
                self.skipInstruction(userData)
                return

        except Exception as e:
            logging.debug("exception in _guidedHook @%s: %s" % (self.hexString(address), e))
            print("exception in _guidedHook @%s: %s" % (self.hexString(address), e))
            self.stopEmulation(userData)

    # scans ahead from address until IDA finds an instruction
    def _scanForCode(self, address):
        while idc.print_insn_mnem(address) == "":
            address = idc.next_head(address, idc.get_inf_attr(idc.INF_MAX_EA))
        return address

    # checks ARM mode for address and aligns address accordingly
    def _handleThumbMode(self, address):
        if self.isThumbMode(address):
            self.uc.reg_write(self.regs["pc"], self.getRegVal("pc") | 1)
            self.mode = unicorn.UC_MODE_THUMB
        else:
            self.uc.reg_write(self.regs["pc"], self.getRegVal("pc") & ~1)
            self.mode = unicorn.UC_MODE_ARM

    # called when an iterate target is reached
    def _targetHit(self, address, userData):
        try:
            argv = self.getArgv()
            userData["targetCallback"](self, address, argv, userData)
        except Exception as e:
            logging.debug("exception in targetCallback function @%s: %s" % (self.hexString(address), str(e)))
            print("exception in targetCallback function @%s: %s" % (self.hexString(address), str(e)))
        userData["visitedTargets"].append(address)

    def _isBadBranch(self, userData):
        if self.arch == unicorn.UC_ARCH_ARM64:
            if (idc.print_insn_mnem(userData["currAddr"]) in ["BR", "BREQ"] and
                    idc.get_operand_type(userData["currAddr"], 0) == 1):
                if (idc.print_insn_mnem(
                        self.uc.reg_read(
                        self.regs[idc.print_operand(userData["currAddr"], 0)]
                        ))) == "":
                    return True
        elif self.arch == unicorn.UC_ARCH_X86:
            if (idc.print_insn_mnem(userData["currAddr"]) == "jmp" and
                    idc.get_operand_type(userData["currAddr"], 0) == 1):
                if (idc.print_insn_mnem
                   (self.uc.reg_read(self.regs[idc.print_operand(userData["currAddr"], 0)])) == ""):
                    logging.debug("bad branch detected @%s" % self.hexString(userData["currAddr"]))
                    return True

    # returns a list of lists containing all possible paths through a function as basic block ids
    # or returns the index of a single list for the first path found leading to end_bb
    def _explore(self, start_bb, end_bb=None):
        if hasattr(self, 'explorePaths') is False:
            self.explorePaths = [[]]
            self.explorePathIdx = 0

        # set_cmt(start_bb.start_ea, "%d" % start_bb.id, 0)

        # optional target bb found, back out
        if end_bb is not None and end_bb == start_bb.id:
            self.explorePaths[self.explorePathIdx].append(start_bb.id)
            return self.explorePathIdx

        # handle loops by treating them like a terminating bb
        if start_bb.id in self.explorePaths[self.explorePathIdx]:
            # forging a new path
            self.explorePaths.append(
                deepcopy(self.explorePaths[self.explorePathIdx]))
            self.explorePathIdx += 1
            return None

        # add this bb to our list
        self.explorePaths[self.explorePathIdx].append(start_bb.id)

        # if we are a terminating bb stop recursing
        if self.isTerminatingBB(start_bb):
            # forging a new path
            self.explorePaths.append(
                deepcopy(self.explorePaths[self.explorePathIdx]))
            self.explorePathIdx += 1
            self.explorePaths[self.explorePathIdx].pop()
            return None

        # visit successor bbs
        for w in start_bb.succs():
            r = self._explore(w, end_bb)
            if r is not None:
                return r
        self.explorePaths[self.explorePathIdx].pop()

    def _findUnusedMemRegion(self):
        # start at 0x10000 to avoid collision with null mem references during emulation
        highest = 0x10000
        for region in self.uc.mem_regions():
            if region[1] > highest:
                highest = region[1]
        for segVA in idautils.Segments():
            endVA = idc.get_segm_end(segVA)
            if endVA > highest:
                highest = endVA
        highest += PAGESIZE
        return self.pageAlignUp(highest)

    # stack setup
    # stack pointer will begin in the middle of allocated stack size
    def _buildStack(self):
        self.stack = self.allocEmuMem(self.stackSize) + self.stackSize / 2
        self.uc.mem_write(self.stack - self.stackSize /
                          2, "\x00" * self.stackSize)

    def _enableVFP(self):
        if self.arch == unicorn.UC_ARCH_ARM:
            # for ARM, we must run this code in order to enable vector instructions in our emulator
            """
            mov.w r0, #0xf00000
            mcr p15, #0x0, r0, c1, c0, #0x2
            isb sy
            mov.w r3, #0x40000000
            vmsr fpexc, r3
            """
            # ENABLE_VFP_CODE = "\x0f\x06\xa0\xe3\x50\x0f\x01\xee\x6f\xf0\x7f\xf5\x01\x31\xa0\xe3\x10\x3a\xe8\xee"
            # self.emulateBytes(ENABLE_VFP_CODE, {}, [])
            tmp = self.uc.reg_read(unicorn.arm_const.UC_ARM_REG_C1_C0_2)
            self.uc.reg_write(unicorn.arm_const.UC_ARM_REG_C1_C0_2, tmp | (0xf << 20))
            self.uc.reg_write(unicorn.arm_const.UC_ARM_REG_FPEXC, 0x40000000)
        elif self.arch == unicorn.UC_ARCH_ARM64:
            """
            https://static.docs.arm.com/ddi0487/ca/DDI0487C_a_armv8_arm.pdf
            MRS X2, CPACR_EL1
            ORR X2, X2, #0x300000 # <-- set bits 20,21 to disable trapping for FP related instructions
            MSR  CPACR_EL1, X2
            NOP # <-- handle Unicorn bug
            """
            ENABLE_VFP_CODE = "\x42\x10\x38\xd5\x42\x04\x6c\xb2\x42\x10\x18\xd5\x1f\x20\x03\xd5"
            self.emulateBytes(ENABLE_VFP_CODE)

    # prepare thread context
    def _prepEmuContext(self, registers, stack):
        mu = self.uc
        for reg in self.regs:
            mu.reg_write(self.regs[reg], 0)
        mu.reg_write(self.regs["sp"], self.stack)
        for reg in registers:
            val = registers[reg]
            if isinstance(val, str):
                mem = self.allocEmuMem(len(val))
                mu.mem_write(mem, val)
                val = mem
            elif isinstance(val, (int, long)):
                pass
            else:
                logging.debug("incorrect type for %s" % reg)
                return None
            mu.reg_write(self.regs[reg], val)
            registers[reg] = val

        # setup stack
        for i in range(0, len(stack)):
            if isinstance(stack[i], str):
                mem = self.allocEmuMem(len(stack[i]))
                mu.mem_write(mem, stack[i])
                stack[i] = mem
                val = mem
            elif isinstance(stack[i], (int, long)):
                val = stack[i]
            else:
                logging.debug("incorrect type for stack[%d]" % (i))
                return None

            mu.mem_write(self.getRegVal("sp") + i *
                         self.size_pointer, struct.pack(self.pack_fmt, val))
