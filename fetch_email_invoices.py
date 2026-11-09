#!/usr/bin/env python3
"""Compatibility forwarder -> finance/fetch_email_invoices.py"""
import os
import sys

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance", "fetch_email_invoices.py")
os.execv(sys.executable, [sys.executable, TARGET] + sys.argv[1:])
