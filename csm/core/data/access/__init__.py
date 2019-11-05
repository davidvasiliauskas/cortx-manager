#!/usr/bin/env python3

"""
 ****************************************************************************
 Filename:          __init__.py
 _description:      Init file for access module

 Creation Date:     6/10/2019
 Author:            Dmitry Didenko
                    Alexander Nogikh

 Do NOT modify or remove this copyright and confidentiality notice!
 Copyright (c) 2001 - $Date: 2015/01/14 $ Seagate Technology, LLC.
 The code contained herein is CONFIDENTIAL to Seagate Technology, LLC.
 Portions are also trade secret. Any use, duplication, derivation, distribution
 or disclosure of this code, for any reason, not expressly authorized is
 prohibited. All other rights are expressly reserved by Seagate Technology, LLC.
 ****************************************************************************
"""

from csm.core.data.access.filters import IFilter, IFilterTreeVisitor
from csm.core.data.access.filters import And, Or, Compare
from csm.core.data.access.queries import Query, ExtQuery, SortOrder
from csm.core.data.access.storage import IDataBase